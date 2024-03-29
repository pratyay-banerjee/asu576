import logging
from typing import Any, Dict, List, Optional

import torch
from torch.nn.functional import nll_loss

from allennlp.common.checks import check_dimensions_match
from allennlp.data import Vocabulary
from allennlp.models.model import Model
from allennlp.modules import Highway
from allennlp.modules import Seq2SeqEncoder, SimilarityFunction, TimeDistributed, TextFieldEmbedder
from allennlp.modules.matrix_attention.legacy_matrix_attention import LegacyMatrixAttention
from allennlp.nn import util, InitializerApplicator, RegularizerApplicator
from allennlp.training.metrics import BooleanAccuracy, CategoricalAccuracy, SquadEmAndF1
from torch.nn.functional import log_softmax
from torch.nn import Softmax, CrossEntropyLoss
from allennlp.modules.seq2seq_encoders import StackedSelfAttentionEncoder, MultiHeadSelfAttention

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


@Model.register("bidafselfattentmh")
class BidirectionalAttentionFlow(Model):
    """
    This class implements Minjoon Seo's `Bidirectional Attention Flow model
    <https://www.semanticscholar.org/paper/Bidirectional-Attention-Flow-for-Machine-Seo-Kembhavi/7586b7cca1deba124af80609327395e613a20e9d>`_
    for answering reading comprehension questions (ICLR 2017).

    The basic layout is pretty simple: encode words as a combination of word embeddings and a
    character-level encoder, pass the word representations through a bi-LSTM/GRU, use a matrix of
    attentions to put question information into the passage word representations (this is the only
    part that is at all non-standard), pass this through another few layers of bi-LSTMs/GRUs, and
    do a softmax over span start and span end.

    Parameters
    ----------
    vocab : ``Vocabulary``
    text_field_embedder : ``TextFieldEmbedder``
        Used to embed the ``question`` and ``passage`` ``TextFields`` we get as input to the model.
    num_highway_layers : ``int``
        The number of highway layers to use in between embedding the input and passing it through
        the phrase layer.
    phrase_layer : ``Seq2SeqEncoder``
        The encoder (with its own internal stacking) that we will use in between embedding tokens
        and doing the bidirectional attention.
    similarity_function : ``SimilarityFunction``
        The similarity function that we will use when comparing encoded passage and question
        representations.
    self_attention_layer : ``StackedSelfAttentionEncoder``
        SelfAttention Text Encoder
    modeling_layer : ``Seq2SeqEncoder``
        The encoder (with its own internal stacking) that we will use in between the bidirectional
        attention and predicting span start and end.
    span_end_encoder : ``Seq2SeqEncoder``
        The encoder that we will use to incorporate span start predictions into the passage state
        before predicting span end.
    dropout : ``float``, optional (default=0.2)
        If greater than 0, we will apply dropout with this probability after all encoders (pytorch
        LSTMs do not apply dropout to their last layer).
    mask_lstms : ``bool``, optional (default=True)
        If ``False``, we will skip passing the mask to the LSTM layers.  This gives a ~2x speedup,
        with only a slight performance decrease, if any.  We haven't experimented much with this
        yet, but have confirmed that we still get very similar performance with much faster
        training times.  We still use the mask for all softmaxes, but avoid the shuffling that's
        required when using masking with pytorch LSTMs.
    initializer : ``InitializerApplicator``, optional (default=``InitializerApplicator()``)
        Used to initialize the model parameters.
    regularizer : ``RegularizerApplicator``, optional (default=``None``)
        If provided, will be used to calculate the regularization penalty during training.
    """
    def __init__(self, vocab: Vocabulary,
                 text_field_embedder: TextFieldEmbedder,
                 num_highway_layers: int,
                 phrase_layer: Seq2SeqEncoder,
                 similarity_function: SimilarityFunction,
                 multi_headed_attention_layer : MultiHeadSelfAttention,
                 modeling_layer: Seq2SeqEncoder,
                 span_end_encoder: Seq2SeqEncoder,
                 dropout: float = 0.2,
                 mask_lstms: bool = True,
                 initializer: InitializerApplicator = InitializerApplicator(),
                 regularizer: Optional[RegularizerApplicator] = None) -> None:
        super(BidirectionalAttentionFlow, self).__init__(vocab, regularizer)

        self._text_field_embedder = text_field_embedder
        self._highway_layer = TimeDistributed(Highway(text_field_embedder.get_output_dim(),
                                                      num_highway_layers))
        self._phrase_layer = phrase_layer
        self._matrix_attention = LegacyMatrixAttention(similarity_function)
        self._modeling_layer = modeling_layer
        self._span_end_encoder = span_end_encoder
        
        #New Self Attention layer
        self._self_attention_layer = multi_headed_attention_layer
        self._sa_matrix_attention = LegacyMatrixAttention(similarity_function)
        selfattent_dim = multi_headed_attention_layer.get_output_dim()
        #print("Self Attention Output Dim:",selfattent_dim,"\n")

        encoding_dim = phrase_layer.get_output_dim()
        modeling_dim = modeling_layer.get_output_dim()
        #span_start_input_dim = encoding_dim * 4 + modeling_dim
        
        span_start_input_dim = encoding_dim * 4 + modeling_dim + 2*selfattent_dim
        
        self._span_start_predictor = TimeDistributed(torch.nn.Linear(span_start_input_dim, 1))

        span_end_encoding_dim = span_end_encoder.get_output_dim()
        #span_end_input_dim = encoding_dim * 4 + span_end_encoding_dim
        span_end_input_dim = encoding_dim * 4 + span_end_encoding_dim + 2*selfattent_dim

        self._span_end_predictor = TimeDistributed(torch.nn.Linear(span_end_input_dim, 1))

        # Bidaf has lots of layer dimensions which need to match up - these aren't necessarily
        # obvious from the configuration files, so we check here.
        check_dimensions_match(modeling_layer.get_input_dim(), 4 * encoding_dim + 2*selfattent_dim,
                               "modeling layer input dim", "4 * encoding dim")
        check_dimensions_match(text_field_embedder.get_output_dim(), phrase_layer.get_input_dim(),
                               "text field embedder output dim", "phrase layer input dim")
        check_dimensions_match(span_end_encoder.get_input_dim(), 4 * encoding_dim + 3 * modeling_dim +2*selfattent_dim,
                               "span end encoder input dim", "4 * encoding dim + 3 * modeling dim")

        self._na_accuracy = CategoricalAccuracy()
        self._span_start_accuracy = CategoricalAccuracy()
        self._span_end_accuracy = CategoricalAccuracy()
        self._span_accuracy = BooleanAccuracy()
        self._squad_metrics = SquadEmAndF1()

        self._na_dense = lambda in_dim : torch.nn.Linear(in_dim, 2).cuda()

        if dropout > 0:
            self._dropout = torch.nn.Dropout(p=dropout)
        else:
            self._dropout = lambda x: x
        self._mask_lstms = mask_lstms

        initializer(self)

    def forward(self,  # type: ignore
                question: Dict[str, torch.LongTensor],
                passage: Dict[str, torch.LongTensor],
                span_start: torch.IntTensor = None,
                span_end: torch.IntTensor = None,
                metadata: List[Dict[str, Any]] = None) -> Dict[str, torch.Tensor]:
        # pylint: disable=arguments-differ
        """
        Parameters
        ----------
        question : Dict[str, torch.LongTensor]
            From a ``TextField``.
        passage : Dict[str, torch.LongTensor]
            From a ``TextField``.  The model assumes that this passage contains the answer to the
            question, and predicts the beginning and ending positions of the answer within the
            passage.
        span_start : ``torch.IntTensor``, optional
            From an ``IndexField``.  This is one of the things we are trying to predict - the
            beginning position of the answer with the passage.  This is an `inclusive` token index.
            If this is given, we will compute a loss that gets included in the output dictionary.
        span_end : ``torch.IntTensor``, optional
            From an ``IndexField``.  This is one of the things we are trying to predict - the
            ending position of the answer with the passage.  This is an `inclusive` token index.
            If this is given, we will compute a loss that gets included in the output dictionary.
        metadata : ``List[Dict[str, Any]]``, optional
            If present, this should contain the question ID, original passage text, and token
            offsets into the passage for each instance in the batch.  We use this for computing
            official metrics using the official SQuAD evaluation script.  The length of this list
            should be the batch size, and each dictionary should have the keys ``id``,
            ``original_passage``, and ``token_offsets``.  If you only want the best span string and
            don't care about official metrics, you can omit the ``id`` key.

        Returns
        -------
        An output dictionary consisting of:
        span_start_logits : torch.FloatTensor
            A tensor of shape ``(batch_size, passage_length)`` representing unnormalized log
            probabilities of the span start position.
        span_start_probs : torch.FloatTensor
            The result of ``softmax(span_start_logits)``.
        span_end_logits : torch.FloatTensor
            A tensor of shape ``(batch_size, passage_length)`` representing unnormalized log
            probabilities of the span end position (inclusive).
        span_end_probs : torch.FloatTensor
            The result of ``softmax(span_end_logits)``.
        best_span : torch.IntTensor
            The result of a constrained inference over ``span_start_logits`` and
            ``span_end_logits`` to find the most probable span.  Shape is ``(batch_size, 2)``
            and each offset is a token index.
        loss : torch.FloatTensor, optional
            A scalar loss to be optimised.
        best_span_str : List[str]
            If sufficient metadata was provided for the instances in the batch, we also return the
            string from the original passage that the model thinks is the best answer to the
            question.
        """

        embedded_question = self._highway_layer(self._text_field_embedder(question))
        embedded_passage = self._highway_layer(self._text_field_embedder(passage))
        batch_size = embedded_question.size(0)
        passage_length = embedded_passage.size(1)
        question_mask = util.get_text_field_mask(question).float()
        passage_mask = util.get_text_field_mask(passage).float()
        question_lstm_mask = question_mask if self._mask_lstms else None
        passage_lstm_mask = passage_mask if self._mask_lstms else None

        encoded_question = self._dropout(self._phrase_layer(embedded_question, question_lstm_mask))
        encoded_passage = self._dropout(self._phrase_layer(embedded_passage, passage_lstm_mask))
        encoding_dim = encoded_question.size(-1)

        #New Question SA encoding
        sa_encoded_question = self._self_attention_layer(embedded_question, question_lstm_mask)
        sa_encoded_passage = self._self_attention_layer(embedded_passage, passage_lstm_mask)

        # Shape: (batch_size, passage_length, question_length)
        passage_question_similarity = self._matrix_attention(encoded_passage, encoded_question)
        sa_passage_question_similarity = self._sa_matrix_attention(sa_encoded_passage, sa_encoded_question)
        
        # Shape: (batch_size, passage_length, question_length)
        #print(passage_question_similarity.size())
        question_mask_temp = question_mask
        question_mask_temp = question_mask_temp.unsqueeze_(1)
        # print(question_mask_temp.size())

        # print(question_mask_temp.size())
        passage_question_attention = util.masked_softmax(passage_question_similarity, question_mask_temp)
        sa_passage_question_attention = util.masked_softmax(sa_passage_question_similarity, question_mask_temp)
        
        # Shape: (batch_size, passage_length, encoding_dim)
        passage_question_vectors = util.weighted_sum(encoded_question, passage_question_attention)
        sa_passage_question_vectors = util.weighted_sum(sa_encoded_question, sa_passage_question_attention)

        # We replace masked values with something really negative here, so they don't affect the
        # max below.
        masked_similarity = util.replace_masked_values(passage_question_similarity,
                                                       question_mask,
                                                       -1e7)
        # Shape: (batch_size, passage_length)
        question_passage_similarity = masked_similarity.max(dim=-1)[0].squeeze(-1)
        # Shape: (batch_size, passage_length)
        question_passage_attention = util.masked_softmax(question_passage_similarity, passage_mask)
        # Shape: (batch_size, encoding_dim)
        question_passage_vector = util.weighted_sum(encoded_passage, question_passage_attention)
        # Shape: (batch_size, passage_length, encoding_dim)
        tiled_question_passage_vector = question_passage_vector.unsqueeze(1).expand(batch_size,
                                                                                    passage_length,
                                                                                    encoding_dim)
        
        #print("Shape of SA Encoded:",sa_encoded_question.size(),sa_encoded_passage.size())
        #print("Required Shape of Encoded Passage:",encoded_passage.size(),passage_question_vectors.size())
        
        # Shape: (batch_size, passage_length, encoding_dim * 4 + 2*sa_dim )
        final_merged_passage = torch.cat([encoded_passage,
                                          sa_encoded_passage,
                                          sa_passage_question_vectors,
                                          passage_question_vectors,
                                          encoded_passage * passage_question_vectors,
                                          encoded_passage * tiled_question_passage_vector],
                                         dim=-1)

        modeled_passage = self._dropout(self._modeling_layer(final_merged_passage, passage_lstm_mask))
        modeling_dim = modeled_passage.size(-1)

        # Shape: (batch_size, passage_length, encoding_dim * 4 + modeling_dim + 2*selfattention_dim))
        span_start_input = self._dropout(torch.cat([final_merged_passage, modeled_passage], dim=-1))
        # Shape: (batch_size, passage_length)
        span_start_logits = self._span_start_predictor(span_start_input).squeeze(-1)
        # Shape: (batch_size, passage_length)
        span_start_probs = util.masked_softmax(span_start_logits, passage_mask)

        # Shape: (batch_size, modeling_dim)
        span_start_representation = util.weighted_sum(modeled_passage, span_start_probs)
        # Shape: (batch_size, passage_length, modeling_dim)
        tiled_start_representation = span_start_representation.unsqueeze(1).expand(batch_size,
                                                                                   passage_length,
                                                                                   modeling_dim)

        # Shape: (batch_size, passage_length, encoding_dim * 4 + modeling_dim * 3)
        span_end_representation = torch.cat([final_merged_passage,
                                             modeled_passage,
                                             tiled_start_representation,
                                             modeled_passage * tiled_start_representation],
                                            dim=-1)
        # Shape: (batch_size, passage_length, encoding_dim)
        encoded_span_end = self._dropout(self._span_end_encoder(span_end_representation,
                                                                passage_lstm_mask))
        # Shape: (batch_size, passage_length, encoding_dim * 4 + span_end_encoding_dim)
        span_end_input = self._dropout(torch.cat([final_merged_passage, encoded_span_end], dim=-1))
        span_end_logits = self._span_end_predictor(span_end_input).squeeze(-1)
        span_end_probs = util.masked_softmax(span_end_logits, passage_mask)
        span_start_logits = util.replace_masked_values(span_start_logits, passage_mask, -1e7)
        span_end_logits = util.replace_masked_values(span_end_logits, passage_mask, -1e7)
        best_span = self.get_best_span(span_start_logits, span_end_logits)

        span_start_logits_do = self._dropout(span_start_logits)
        na_logits_start = self._na_dense(passage_length)(span_start_logits_do)
        span_end_logits_do = self._dropout(span_end_logits)
        na_logits_end = self._na_dense(passage_length)(span_end_logits_do)
        na_logits = Softmax(dim=1)(na_logits_start) * Softmax(dim=1)(na_logits_end)
        na_probs = Softmax(dim=1)(na_logits)
        na_gt = (span_start == -1)
        na_inv = (1.0 - na_gt)

        output_dict = {
                "passage_question_attention": passage_question_attention,
                "span_start_logits": span_start_logits,
                "span_start_probs": span_start_probs,
                "span_end_logits": span_end_logits,
                "span_end_probs": span_end_probs,
                "best_span": best_span,
                "na_logits": na_logits,
                "na_probs": na_probs
        }

        # Compute the loss for training.
        if span_start is not None:
            loss = 0.0

            # calculate loss for answer existance
            loss += CrossEntropyLoss()(na_probs.type(torch.cuda.FloatTensor), na_gt.squeeze(-1).type(torch.cuda.LongTensor))
            self._na_accuracy(na_probs.type(torch.cuda.FloatTensor), na_gt.squeeze(-1).type(torch.cuda.FloatTensor))

            # calculate loss if there is answer
            # loss for start
            preds_start =(na_inv.type(torch.cuda.FloatTensor) * util.masked_log_softmax(span_start_logits.type(torch.cuda.FloatTensor),passage_mask.type(torch.cuda.FloatTensor))).type(torch.cuda.FloatTensor)
            y_start = (na_inv.squeeze(-1).type(torch.cuda.ByteTensor) * span_start.squeeze(-1).type(torch.cuda.ByteTensor)).type(torch.cuda.LongTensor)
            loss += nll_loss(preds_start, y_start)
            
            # accuracy for start
            acc_p_start = na_inv.type(torch.cuda.FloatTensor) * span_start_logits.type(torch.cuda.FloatTensor)
            acc_y_start = na_inv.squeeze(-1).type(torch.cuda.FloatTensor) * span_start.squeeze(-1).type(torch.cuda.FloatTensor)
            self._span_start_accuracy(acc_p_start, acc_y_start)

            
            # loss for end
            preds_end = (na_inv.type(torch.cuda.FloatTensor) * util.masked_log_softmax(span_end_logits.type(torch.cuda.FloatTensor),passage_mask.type(torch.cuda.FloatTensor))).type(torch.cuda.FloatTensor)
            y_end = (na_inv.squeeze(-1).type(torch.cuda.ByteTensor) * span_end.squeeze(-1).type(torch.cuda.ByteTensor)).type(torch.cuda.LongTensor)            
            loss += nll_loss(preds_end, y_end)
            
            # accuracy for end
            acc_p_end = na_inv.type(torch.cuda.FloatTensor) * span_end_logits.type(torch.cuda.FloatTensor)
            acc_y_end = na_inv.squeeze(-1).type(torch.cuda.FloatTensor) * span_end.squeeze(-1).type(torch.cuda.FloatTensor)                
            self._span_end_accuracy(acc_p_end, acc_y_end)

            # accuracy for span
            acc_p = na_inv.type(torch.cuda.FloatTensor) * best_span.type(torch.cuda.FloatTensor)
            acc_y = na_inv.type(torch.cuda.FloatTensor) * torch.cat([span_start.type(torch.cuda.FloatTensor), span_end.type(torch.cuda.FloatTensor)], -1)
            self._span_accuracy(acc_p,acc_y)

            output_dict["loss"] = loss

        # Compute the EM and F1 on SQuAD and add the tokenized input to the output.
        if metadata is not None:
            output_dict['best_span_str'] = []
            question_tokens = []
            passage_tokens = []
            for i in range(batch_size):
                question_tokens.append(metadata[i]['question_tokens'])
                passage_tokens.append(metadata[i]['passage_tokens'])
                passage_str = metadata[i]['original_passage']
                offsets = metadata[i]['token_offsets']
                predicted_span = tuple(best_span[i].detach().cpu().numpy())
                start_offset = offsets[predicted_span[0]][0]
                end_offset = offsets[predicted_span[1]][1]
                best_span_string = passage_str[start_offset:end_offset]
                output_dict['best_span_str'].append(best_span_string)
                answer_texts = metadata[i].get('answer_texts', [])
                if answer_texts:
                    self._squad_metrics(best_span_string, answer_texts)
            output_dict['question_tokens'] = question_tokens
            output_dict['passage_tokens'] = passage_tokens
        return output_dict

    def get_metrics(self, reset: bool = False) -> Dict[str, float]:
        exact_match, f1_score = self._squad_metrics.get_metric(reset)
        return {
                'na_acc': self._na_accuracy.get_metric(reset),
                'start_acc': self._span_start_accuracy.get_metric(reset),
                'end_acc': self._span_end_accuracy.get_metric(reset),
                'span_acc': self._span_accuracy.get_metric(reset),
                'em': exact_match,
                'f1': f1_score,
                }

    @staticmethod
    def get_best_span(span_start_logits: torch.Tensor, span_end_logits: torch.Tensor) -> torch.Tensor:
        if span_start_logits.dim() != 2 or span_end_logits.dim() != 2:
            raise ValueError("Input shapes must be (batch_size, passage_length)")
        batch_size, passage_length = span_start_logits.size()
        max_span_log_prob = [-1e20] * batch_size
        span_start_argmax = [0] * batch_size
        best_word_span = span_start_logits.new_zeros((batch_size, 2), dtype=torch.long)

        span_start_logits = span_start_logits.detach().cpu().numpy()
        span_end_logits = span_end_logits.detach().cpu().numpy()

        for b in range(batch_size):  # pylint: disable=invalid-name
            for j in range(passage_length):
                val1 = span_start_logits[b, span_start_argmax[b]]
                if val1 < span_start_logits[b, j]:
                    span_start_argmax[b] = j
                    val1 = span_start_logits[b, j]

                val2 = span_end_logits[b, j]

                if val1 + val2 > max_span_log_prob[b]:
                    best_word_span[b, 0] = span_start_argmax[b]
                    best_word_span[b, 1] = j
                    max_span_log_prob[b] = val1 + val2
        return best_word_span
