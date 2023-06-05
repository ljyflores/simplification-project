import argparse
import json
import math
import spacy
import torch

from collections import UserDict
from torch.utils.data import DataLoader
from transformers.generation.beam_search import BeamSearchScorer
from transformers.generation.logits_process import (
    MinLengthLogitsProcessor,
    ForcedBOSTokenLogitsProcessor,
    ForcedEOSTokenLogitsProcessor,
    NoRepeatNGramLogitsProcessor
)
from transformers import (
    AutoTokenizer,
    AutoModelForSeq2SeqLM,
    LogitsProcessorList,
    BeamSearchScorer,
    StoppingCriteriaList,
    MaxLengthCriteria
)
from typing import List, Optional, Tuple, Union
from utils_eval import (
    calculate_fkgl_easse, 
    calculate_bertscore
    )

ner_model = spacy.load("en_core_web_lg")

# Dataset and model
parser = argparse.ArgumentParser()
parser.add_argument("--dataset", required=True, type=str)
parser.add_argument("--model", required=True, type=str)
parser.add_argument("--checkpoint", required=False, type=str, default=None)
parser.add_argument("--suffix", required=False, type=str, default="")
args = parser.parse_args()

class NewBeamScorer(BeamSearchScorer):
    def set_utils(self, tokenizer, ner_model):
        self.tokenizer = tokenizer
        self.ner_model = ner_model

    def set_encoder_input(self, encoder_input):
        self.encoder_input = encoder_input

    def rescale_fk(self, score):
        if score <= 4:
            return 1.0
        elif score <= 10:
            return (10.0 - score) / 6.0
        else:
            return 0.0
        
    def rescale_bs(self, score):
        if score <= 0.60:
            return 0.0
        else:
            return (score-0.60) / 0.40
        
    # def rescale_sari(self, score):
    #     if score <= 20.0:
    #         return 0.0
    #     elif score <= 80.0:
    #         return (score - 20.0) / 60.0
    #     else:
    #         return 1.0

    def rerank(self,
               input_ids: torch.LongTensor, # num_beams x curr_seq_len
               next_scores: torch.LongTensor, # batch_size x 2*num_beams
               next_tokens: torch.LongTensor, # batch_size x 2*num_beams
               next_indices: torch.LongTensor, # batch_size x 2*num_beams
               input_strings: List[str] # batch_size
               ):
        for i in range(next_scores.shape[0]):
            
            current_strings = []
            for j in range(next_scores.shape[1]):
                current_string_ids = torch.cat([input_ids[next_indices[i,j]], 
                                                next_tokens[i,j].unsqueeze(0)])
                current_string = self.tokenizer.decode(current_string_ids)
                current_strings.append(current_string)

            # 1. Score beams based on their FK score
            current_strings_fk = [
                calculate_fkgl_easse(s) \
                    for s in current_strings
                ]
            current_strings_fk = list(map(self.rescale_fk, current_strings_fk))
            
            # 2. Score beams based on their BERTScore
            current_strings_bs = [
                calculate_bertscore(
                    predictions=[s],
                    references=[input_strings[i]]
                    ) for s in current_strings
            ]
            current_strings_bs = list(map(self.rescale_bs, current_strings_bs))

            # TO TUNE
            # Use sqrt scaling
            # current_strings_fk   = [math.sqrt(x) for x in current_strings_fk]
            # current_strings_sari = [math.sqrt(x) for x in current_strings_sari]
            current_scores = [(2*a*b)/(a+b) if a+b>0.0 else 0.0 for (a,b) \
                              in zip(current_strings_fk, current_strings_bs)]

            # 3. Kill a beam if it has an unsupported entity
            for j in range(next_scores.shape[1]):
                entities = [str(s).lower() for s in self.ner_model(current_strings[j]).ents]
                # print(current_strings[j], entities)
                if any([s not in input_strings[i].lower() for s in entities]):
                    # beam_scores[beam_scores.argmax()] = float('-inf')
                    current_scores[j] = 0 # Kill the entire beam

            # print([(a,b,c,d) for (a,b,c,d) in zip(current_strings, 
            #                                       current_scores, 
            #                                       current_strings_fk, 
            #                                       current_strings_sari)])
            beam_score_order = torch.argsort(torch.tensor(current_scores), descending=True)
            next_scores[i]   = next_scores[i][beam_score_order]
            next_tokens[i]   = next_tokens[i][beam_score_order]
            next_indices[i]  = next_indices[i][beam_score_order]
            
        return next_scores, next_tokens, next_indices

    def process(
        self,
        input_ids: torch.LongTensor,
        next_scores: torch.FloatTensor,
        next_tokens: torch.LongTensor,
        next_indices: torch.LongTensor,
        pad_token_id: Optional[int] = None,
        eos_token_id: Optional[Union[int, List[int]]] = None,
        beam_indices: Optional[torch.LongTensor] = None,
    ) -> Tuple[torch.Tensor]:
        
        if self.rerank_flag:
            next_scores, next_tokens, next_indices = self.rerank(input_ids, 
                                                                next_scores, 
                                                                next_tokens, 
                                                                next_indices, 
                                                                self.encoder_input)
        # add up to the length which the next_scores is calculated on
        cur_len = input_ids.shape[-1] + 1  
        batch_size = len(self._beam_hyps)
        if not (batch_size == (input_ids.shape[0] // self.group_size)):
            if self.num_beam_groups > 1:
                raise ValueError(
                    f"A group beam size of {input_ids.shape[0]} is used"
                    "as the input, but a group beam "
                    f"size of {self.group_size} is"
                    "expected by the beam scorer."
                )
            else:
                raise ValueError(
                    f"A beam size of {input_ids.shape[0]} is used"
                    "as the input, but a beam size of "
                    f"{self.group_size} is expected by the beam scorer."
                )

        device = input_ids.device
        next_beam_scores = torch.zeros((batch_size, self.group_size), dtype=next_scores.dtype, device=device)
        next_beam_tokens = torch.zeros((batch_size, self.group_size), dtype=next_tokens.dtype, device=device)
        next_beam_indices = torch.zeros((batch_size, self.group_size), dtype=next_indices.dtype, device=device)

        if isinstance(eos_token_id, int):
            eos_token_id = [eos_token_id]
        
        for batch_idx, beam_hyp in enumerate(self._beam_hyps):
            if self._done[batch_idx]:
                if self.num_beams < len(beam_hyp):
                    raise ValueError(f"Batch can only be done if at least"
                                     "{self.num_beams} beams have been generated")
                if eos_token_id is None or pad_token_id is None:
                    raise ValueError("Generated beams >= num_beams -> eos_token_id"
                                     "and pad_token have to be defined")
                # pad the batch
                next_beam_scores[batch_idx, :] = 0
                next_beam_tokens[batch_idx, :] = pad_token_id
                next_beam_indices[batch_idx, :] = 0
                continue

            # next tokens for this sentence
            beam_idx = 0
            for beam_token_rank, (next_token, next_score, next_index) in enumerate(
                zip(next_tokens[batch_idx], next_scores[batch_idx], next_indices[batch_idx])
            ):
                batch_beam_idx = batch_idx * self.group_size + next_index
                # add to generated hypotheses if end of sentence
                if (eos_token_id is not None) and (next_token.item() in eos_token_id):
                    # if beam_token does not belong to top num_beams tokens, it should not be added
                    is_beam_token_worse_than_top_num_beams = beam_token_rank >= self.group_size
                    if is_beam_token_worse_than_top_num_beams:
                        continue
                    if beam_indices is not None:
                        beam_index = beam_indices[batch_beam_idx]
                        beam_index = beam_index + (batch_beam_idx,)
                    else:
                        beam_index = None

                    beam_hyp.add(
                        input_ids[batch_beam_idx].clone(),
                        next_score.item(),
                        beam_indices=beam_index,
                    )
                else:
                    # add next predicted token since it is not eos_token
                    next_beam_scores[batch_idx, beam_idx] = next_score
                    next_beam_tokens[batch_idx, beam_idx] = next_token
                    next_beam_indices[batch_idx, beam_idx] = batch_beam_idx
                    beam_idx += 1

                # once the beam for next step is full, don't add more tokens to it.
                if beam_idx == self.group_size:
                    break

            if beam_idx < self.group_size:
                raise ValueError(
                    f"At most {self.group_size} tokens in {next_tokens[batch_idx]} can be equal to `eos_token_id:"
                    f" {eos_token_id}`. Make sure {next_tokens[batch_idx]} are corrected."
                )

            # Check if we are done so that we can save a pad step if all(done)
            self._done[batch_idx] = self._done[batch_idx] or beam_hyp.is_done(
                next_scores[batch_idx].max().item(), cur_len
            )

        return UserDict(
            {
                "next_beam_scores": next_beam_scores.view(-1),
                "next_beam_tokens": next_beam_tokens.view(-1),
                "next_beam_indices": next_beam_indices.view(-1),
            }
        )
    

model_name_dict = {
    "bart": ("BART", "facebook/bart-large"),
    "bart_xsum": ("BART_XSUM", "facebook/bart-large-xsum"),
    "flant5": ("FLANT5_LARGE", "google/flan-t5-large"),
    "flant5_base": ("FLANT5_BASE", "google/flan-t5-base"),
}

tokenizer = AutoTokenizer.from_pretrained(model_name_dict[args.model][1])
model = AutoModelForSeq2SeqLM.from_pretrained(model_name_dict[args.model][1]\
                                            if args.checkpoint == None \
                                            else args.checkpoint).cuda()

ner_model = spacy.load("en_core_web_lg")

df = json.load(open(f"data/{args.dataset}_multiple.json"))
dataloader = DataLoader(df["test"], batch_size=1)

# instantiate logits processors
logits_processor = LogitsProcessorList([ForcedBOSTokenLogitsProcessor(bos_token_id=tokenizer.bos_token_id),
                                        ForcedEOSTokenLogitsProcessor(max_length=768, eos_token_id=tokenizer.eos_token_id),
                                        NoRepeatNGramLogitsProcessor(ngram_size=3)])

model.eval()
# output_list = []

for idx, batch in enumerate(dataloader):
    
    print(f"Now on id: {idx}")

    # instantiate beam scorer
    num_beams = 2
    beam_scorer = NewBeamScorer(
        batch_size=1,
        num_beams=num_beams,
        device=model.device,
    )

    beam_scorer.set_utils(tokenizer, ner_model)
    beam_scorer.rerank_flag = True

    # encode input
    text = batch["input"][0]
    encoder_input_ids = tokenizer(text,
                                  truncation=True,
                                  max_length=1024,
                                return_tensors="pt").input_ids.cuda()
    
    # add encoder_outputs to model keyword arguments
    model_kwargs = {
        "encoder_outputs": model.get_encoder()(
            encoder_input_ids.repeat_interleave(num_beams, dim=0), 
            return_dict=True
        )
    }

    # define decoder start token ids
    input_ids = torch.ones((num_beams, 1), device=model.device, dtype=torch.long)
    input_ids = input_ids * model.config.decoder_start_token_id

    # run beam search
    beam_scorer.set_encoder_input([text])
    outputs = model.beam_search(input_ids, 
                                beam_scorer, 
                                logits_processor=logits_processor, 
                                stopping_criteria=StoppingCriteriaList([
                                    MaxLengthCriteria(max_length=400)
                                    ]),
                                **model_kwargs)

    decoded_outputs = tokenizer.batch_decode(outputs, skip_special_tokens=True)
    # output_list.append(decoded_outputs[0])

    file1 = open(f"output/decode/{args.dataset}_{args.model}{args.suffix}.txt", "a")  # append mode
    file1.write(f"{decoded_outputs[0]}\n")
    file1.close()

# # Write output
# with open(f"output/decode_{args.dataset}_{args.model}{args.suffix}.txt", "w") as fp:
#     for item in output_list:
#         fp.write("%s\n" % item)
#     print("Done")
