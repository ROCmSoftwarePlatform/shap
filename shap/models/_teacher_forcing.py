import numpy as np
import scipy as sp
from ._model import Model
from ..utils import safe_isinstance, record_import_error
from ..utils.transformers import parse_prefix_suffix_for_tokenizer
from .. import models
from .._serializable import Serializer, Deserializer

try:
    import torch
except ImportError as e:
    record_import_error("torch", "Torch could not be imported!", e)

try:
    import tensorflow as tf
except ImportError as e:
    record_import_error("tensorflow", "TensorFlow could not be imported!", e)

class TeacherForcing(Model):
    """ Generates scores (log odds) for output text explanation algorithms using Teacher Forcing technique.

    This class supports generation of log odds for transformer models as well as functions. In model agnostic
    cases (model is function) it expects a similarity_model and similarity_tokenizer to approximate log odd scores
    for target sentence generated by the model.
    """

    def __init__(self, model, tokenizer=None, similarity_model=None, similarity_tokenizer=None, batch_size=128, device=None):
        """ Build a teacher forcing model from the given text generation model.

        Parameters
        ----------
        model: object or function
            A object of any pretrained transformer model or function which is to be explained.

        tokenizer: object
            A tokenizer object(PreTrainedTokenizer/PreTrainedTokenizerFast) which is used to tokenize source and target sentence.

        similarity_model: object
            A pretrained transformer model object which is used in model agnostic scenario to approximate log odds.

        similarity_tokenizer: object
            A tokenizer object(PreTrainedTokenizer/PreTrainedTokenizerFast) which is used to tokenize sentence in model agnostic scenario.

        batch_size: int
            Batch size for model inferencing and computing logodds (default=128).

        device: str
            By default, it infers if system has a gpu and accordingly sets device. Should be 'cpu' or 'cuda' or pytorch models.

        Returns
        -------
        numpy.ndarray
            The scores (log odds) of generating target sentence ids using the model.
        """
        super().__init__(model)

        self.tokenizer = tokenizer
        # set pad token if not defined
        if self.tokenizer is not None and self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token

        self.device = device
        self.batch_size = batch_size
        # assign text generation function
        if safe_isinstance(model, "transformers.PreTrainedModel") or safe_isinstance(model, "transformers.TFPreTrainedModel"):
            self.text_generate = models.TextGeneration(self.inner_model, tokenizer=self.tokenizer, device=self.device)
            self.similarity_model = model
            self.similarity_tokenizer = tokenizer
            self.model_agnostic = False
        else:
            self.text_generate = models.TextGeneration(self.inner_model, device=self.device)
            self.similarity_model = similarity_model
            self.similarity_tokenizer = similarity_tokenizer
            # set pad token for a similarity tokenizer(in a model agnostic scenario) if not defined
            if self.similarity_tokenizer is not None and self.similarity_tokenizer.pad_token is None:
                self.similarity_tokenizer.pad_token = self.similarity_tokenizer.eos_token
            self.model_agnostic = True
        # initializing target which is the target sentence/ids for every new row of explanation
        self.output = None
        self.output_names = None

        self.similarity_model_type = None
        if safe_isinstance(self.similarity_model, "transformers.PreTrainedModel"):
            self.similarity_model_type = "pt"
            if self.device is not None:# = torch.device('cuda' if torch.cuda.is_available() else 'cpu') if self.device is None else self.device
                d = self.similarity_model.device
                assert d == self.device or str(d) == self.device, "The passed similarity_model must be on the same device!"
                #self.similarity_model = self.similarity_model.to(self.device)
        elif safe_isinstance(self.similarity_model, "transformers.TFPreTrainedModel"):
            self.similarity_model_type = "tf"

    def __call__(self, X, Y):
        """ Computes log odds scores of generating output(text) for a given batch of input(text/image) .

        Parameters
        ----------
        X: numpy.ndarray
            An array containing a list of masked inputs.

        Y: numpy.ndarray
            An array containing a list of target sentence/ids.

        Returns
        -------
        numpy.ndarray
            A numpy array of log odds scores for every input pair (masked_X, X)
        """
        output_batch = None
        # caching updates output names and target sentence ids
        self.update_output_names(Y[:1])
        start_batch_idx, end_batch_idx = 0, len(X)
        while start_batch_idx < end_batch_idx:
            X_batch = X[start_batch_idx:start_batch_idx+self.batch_size]
            Y_batch = Y[start_batch_idx:start_batch_idx+self.batch_size]
            logits = self.get_teacher_forced_logits(X_batch, Y_batch)
            logodds = self.get_logodds(logits)
            if output_batch is None:
                output_batch = logodds
            else:
                output_batch = np.concatenate((output_batch, logodds))
            start_batch_idx += self.batch_size
        return output_batch

    def update_output_names(self, output):
        """ The function updates output tokens.

        It mimics the caching mechanism to update the output tokens for every
        new row of explanation that are to be explained.

        Parameters
        ----------
        output: numpy.ndarray
            Output(sentence/sentence ids) for an explanation row.
        """
        # check if the target sentence has been updated (occurs when explaining a new row)
        if (self.output is None) or (not np.array_equal(self.output, output)):
            self.output = output
            self.output_names = self.get_output_names(output)

    def get_output_names(self, output):
        """ Gets the output tokens by computing the output sentence ids and output names using the similarity_tokenizer.

        Parameters
        ----------
        output: numpy.ndarray
            Output(sentence/sentence ids) for an explanation row.

        Returns
        -------
        list
            A list of output tokens.
        """
        output_ids = self.get_outputs(output)
        output_names = [self.similarity_tokenizer.decode([x]).strip() for x in output_ids[0, :]]
        return output_names

    def get_outputs(self, X):
        """ The function tokenizes output sentences and returns ids.

        Parameters
        ----------
        X: numpy.ndarray
            Output(sentence/sentence ids) for an explanation row.

        Returns
        -------
        numpy.ndarray
            An array of output(target sentence) ids.
        """
        # check if output is a sentence or already parsed target ids
        if X.dtype.type is np.str_:
            parsed_tokenizer_dict = parse_prefix_suffix_for_tokenizer(self.similarity_tokenizer)
            keep_prefix, keep_suffix = parsed_tokenizer_dict['keep_prefix'], parsed_tokenizer_dict['keep_suffix']
            if keep_suffix > 0:
                output_ids = np.array(self.similarity_tokenizer(X.tolist(), padding=True)["input_ids"])[:, keep_prefix:-keep_suffix]
            else:
                output_ids = np.array(self.similarity_tokenizer(X.tolist(), padding=True)["input_ids"])[:, keep_prefix:]
        else:
            output_ids = X
        return output_ids

    def get_inputs(self, X, padding_side='right'):
        """ The function tokenizes source sentences.

        In model agnostic case, the function calls model(X) which is expected to
        return a batch of output sentences which is tokenized to compute inputs.

        Parameters
        ----------
        X: numpy.ndarray
            X could be a batch of text or images(model agnostic case).

        Returns
        -------
        dict
            Dictionary of padded source sentence ids and attention mask as tensors("pt" or "tf" based on similarity_model_type).
        """
        if self.model_agnostic:
            # In model agnostic case, we first pass the input through the model and then tokenize output sentence
            input_sentences = np.array(self.inner_model(X))
        else:
            input_sentences = np.array(X)
        # set tokenizer padding to prepare inputs for batch inferencing
        # padding_side="left" for only decoder models text generation eg. GPT2
        self.similarity_tokenizer.padding_side = padding_side
        inputs = self.similarity_tokenizer(input_sentences.tolist(), return_tensors=self.similarity_model_type, padding=True)
        # set tokenizer padding to default
        self.similarity_tokenizer.padding_side = 'right'
        return inputs

    def get_logodds(self, logits):
        """ Calculates log odds from logits.

        This function passes the logits through softmax and then computes log odds for the output(target sentence) ids.

        Parameters
        ----------
        logits: numpy.ndarray
            An array of logits generated from the model.

        Returns
        -------
        numpy.ndarray
            Computes log odds for corresponding output ids.
        """
        # set output ids for which scores are to be extracted
        if self.output.dtype.type is np.str_:
            output_ids = self.get_outputs(self.output)[0]
        else:
            output_ids = self.output[0]

        def calc_logodds(arr):
            probs = np.exp(arr) / np.exp(arr).sum(-1)
            logodds = sp.special.logit(probs)
            return logodds

        # pass logits through softmax, get the token corresponding score and convert back to log odds (as one vs all)
        logodds = np.apply_along_axis(calc_logodds, -1, logits)
        logodds_for_output_ids = logodds[:, np.array(range(logodds.shape[1])), output_ids]
        return logodds_for_output_ids

    def model_inference(self, inputs, output_ids):
        """ This function performs model inference for tensorflow and pytorch models.

        Parameters
        ----------
        inputs: dict
            Dictionary of padded source sentence ids and attention mask as tensors.

        output_ids: numpy.ndarray
            An array of decoder output ids.

        Returns
        -------
        numpy.ndarray
            Returns output logits from the model.
        """
        if self.similarity_model_type == "pt":
            # create torch tensors and move to device
            inputs = inputs.to(self.device)
            output_ids = torch.tensor(output_ids, dtype=torch.int64, device=self.device)
            self.similarity_model.eval()
            with torch.no_grad():
                if self.similarity_model.config.is_encoder_decoder:
                    # model inference
                    outputs = self.similarity_model(**inputs, decoder_input_ids=output_ids, labels=output_ids, return_dict=True)
                else:
                    # combine source and target sentence ids to pass into decoder eg: in case of distillgpt2
                    inputs["input_ids"] = torch.cat((inputs["input_ids"], output_ids), dim=-1)
                    attention_mask_for_output_ids = torch.ones(output_ids.shape, dtype=output_ids.dtype, device=self.device)
                    inputs["attention_mask"] = torch.cat((inputs["attention_mask"], attention_mask_for_output_ids), dim=-1)
                    # create position ids due to left padding for decoder models
                    inputs["position_ids"] = (inputs["attention_mask"].long().cumsum(-1) - 1)
                    inputs["position_ids"].masked_fill_(inputs["attention_mask"] == 0, 0)
                    # model inference
                    outputs = self.similarity_model(**inputs, return_dict=True)
                logits = outputs.logits.detach().cpu().numpy().astype('float64')
        elif self.similarity_model_type == "tf":
            output_ids = tf.convert_to_tensor(output_ids, dtype=tf.int32)
            if self.similarity_model.config.is_encoder_decoder:
                if self.device is None:
                    outputs = self.similarity_model(inputs, decoder_input_ids=output_ids, labels=output_ids, return_dict=True)
                else:
                    try:
                        with tf.device(self.device):
                            outputs = self.similarity_model(inputs, decoder_input_ids=output_ids, labels=output_ids, return_dict=True)
                    except RuntimeError as err:
                        print(err)
            else:
                # combine source and target sentence ids to pass into decoder eg: in case of distillgpt2
                inputs["input_ids"] = tf.concat((inputs["input_ids"], output_ids), axis=-1)
                attention_mask_for_output_ids = tf.ones(output_ids.shape, dtype=output_ids.dtype)
                inputs["attention_mask"] = tf.concat((inputs["attention_mask"], attention_mask_for_output_ids), axis=-1)
                inputs["position_ids"] = tf.math.cumsum(inputs["attention_mask"], axis=-1) - 1
                inputs["position_ids"] = tf.where(inputs["attention_mask"] == 0, 0, inputs["position_ids"])
                if self.device is None:
                    outputs = self.similarity_model(inputs, return_dict=True)
                else:
                    try:
                        with tf.device(self.device):
                            outputs = self.similarity_model(inputs, return_dict=True)
                    except RuntimeError as err:
                        print(err)
            logits = outputs.logits.numpy().astype('float64')
        return logits

    def get_teacher_forced_logits(self, X, Y):
        """ The function generates logits for transformer models.

        It generates logits for encoder-decoder models as well as decoder only models by using the teacher forcing technique.

        Parameters
        ----------
        X: numpy.ndarray
            An array containing a list of masked inputs.

        Y: numpy.ndarray
            An array containing a list of target sentence/ids.

        Returns
        -------
        numpy.ndarray
            Decoder output logits for output(target sentence) ids.
        """
        # check if type of model architecture assigned in model config
        if (hasattr(self.similarity_model.config, "is_encoder_decoder") and not self.similarity_model.config.is_encoder_decoder) \
            and (hasattr(self.similarity_model.config, "is_decoder") and not self.similarity_model.config.is_decoder):
            pass #self.similarity_model.config.is_decoder = True # TODO: is this okay?
            # raise ValueError(
            #     "Please assign either of is_encoder_decoder or is_decoder to True in model config for extracting target sentence ids"
            # )
        # get output ids for teacher forcing
        output_ids = self.get_outputs(Y)
        if self.similarity_model.config.is_encoder_decoder:
            # encode batched inputs by padding on the right side
            inputs = self.get_inputs(X, padding_side='right')
            # assigning decoder start token id as it is needed for encoder decoder model generation
            decoder_start_token_id = None
            if hasattr(self.similarity_model.config, "decoder_start_token_id") and \
                    self.similarity_model.config.decoder_start_token_id is not None:
                decoder_start_token_id = self.similarity_model.config.decoder_start_token_id
            elif hasattr(self.similarity_model.config, "bos_token_id") and self.similarity_model.config.bos_token_id is not None:
                decoder_start_token_id = self.similarity_model.config.bos_token_id
            elif (hasattr(self.similarity_model.config, "decoder") and hasattr(self.similarity_model.config.decoder, "bos_token_id") and \
                    self.similarity_model.config.decoder.bos_token_id is not None):
                decoder_start_token_id = self.similarity_model.config.decoder.bos_token_id
            else:
                raise ValueError(
                    "No decoder_start_token_id or bos_token_id defined in config for encoder-decoder generation"
                )
            # concat decoder start token id to target sentence ids
            output_start_id = np.ones((output_ids.shape[0], 1)) * decoder_start_token_id
            output_ids = np.concatenate((output_start_id, output_ids), axis=-1)
            # generate outputs and logits
            logits = self.model_inference(inputs, output_ids)
            logits = logits[:, :-1, :]
        else:
            # encode batched inputs by padding on the left side
            inputs = self.get_inputs(X, padding_side='left')
            # generate outputs and logits
            logits = self.model_inference(inputs, output_ids)
            # extract only logits corresponding to target sentence ids
            logits = logits[:, -output_ids.shape[1]-1:-1, :]
        return logits

    def save(self, out_file):
        super().save(out_file)

        # Increment the verison number when the encoding changes!
        with Serializer(out_file, "shap.models.TeacherForcing", version=0) as s:
            s.save("tokenizer", self.tokenizer)
            s.save("similarity_model", self.similarity_model)
            s.save("similarity_tokenizer", self.similarity_tokenizer)
            s.save("batch_size", self.batch_size)
            s.save("device", self.device)

    @classmethod
    def load(cls, in_file, instantiate=True):
        if instantiate:
            return cls._instantiated_load(in_file)

        kwargs = super().load(in_file, instantiate=False)
        with Deserializer(in_file, "shap.models.TeacherForcing", min_version=0, max_version=0) as s:
            kwargs["tokenizer"] = s.load("tokenizer")
            kwargs["similarity_model"] = s.load("similarity_model")
            kwargs["similarity_tokenizer"] = s.load("similarity_tokenizer")
            kwargs["batch_size"] = s.load("batch_size")
            kwargs["device"] = s.load("device")
        return kwargs
