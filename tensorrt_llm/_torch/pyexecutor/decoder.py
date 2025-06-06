import itertools
from abc import ABC, abstractmethod
from dataclasses import dataclass

import torch

from tensorrt_llm._utils import torch_dtype_to_binding
from tensorrt_llm.bindings import (DataType, ModelConfig, WorldConfig,
                                   make_sampling_config)
from tensorrt_llm.bindings.executor import (DecodingConfig, DecodingMode,
                                            ExecutorConfig, FinishReason)
from tensorrt_llm.bindings.internal.algorithms import (
    CreateNewDecoderRequests, GenerateRequestOptions, HandleContextLogits,
    HandleGenerationLogits, MakeDecodingBatchInputOutput)
from tensorrt_llm.bindings.internal.batch_manager import (DecoderBuffers,
                                                          DecoderInputBuffers)
from tensorrt_llm.bindings.internal.runtime import (BufferManager, CudaStream,
                                                    GptDecoderBatched,
                                                    SpeculativeDecodingMode)
from tensorrt_llm.mapping import Mapping

from .llm_request import LlmRequest, LlmRequestState, LogProbs
from .scheduler import ScheduledRequests


@dataclass
class DecoderState:
    scheduled_requests: ScheduledRequests

    logits: torch.Tensor = None

    # Set when decode_async() has evaluated these to avoid computing again in update_requests()
    log_probs: list[LogProbs] | None = None

    new_tensors_device: dict[str, torch.Tensor] = None
    new_tensors_host: dict[str, torch.Tensor] = None

    decoder_event: torch.cuda.Event = None


class Decoder(ABC):

    def setup_decoder_step(self, scheduled_requests: ScheduledRequests):
        pass

    @abstractmethod
    def decode_async(self, scheduled_requests: ScheduledRequests,
                     model_outputs) -> DecoderState:
        raise NotImplementedError

    @abstractmethod
    def update_requests(self, decoder_state: DecoderState) -> None:
        raise NotImplementedError


class EarlyStopDecoder(Decoder):
    """
    Use for skipping decoding step for non generation model,
    such as encoder-only model (e.g., BERT) or reward models that only need context phase.
    """

    def decode_async(self, scheduled_requests: ScheduledRequests,
                     model_outputs) -> DecoderState:
        return DecoderState(scheduled_requests=scheduled_requests,
                            logits=model_outputs['logits'])

    def update_requests(self, decoder_state: DecoderState) -> None:
        scheduled_requests = decoder_state.scheduled_requests
        assert (not scheduled_requests.generation_requests)
        for idx, request in enumerate(scheduled_requests.context_requests):
            request.state = LlmRequestState.GENERATION_COMPLETE
            # NOTE: This is a hack: set finish reason manually and set the beam 0
            request.set_finished_reason(FinishReason.LENGTH, 0)
            logits = decoder_state.logits[idx]
            if logits.ndim == 1:
                # For BERT: Add vocab_size axis to be compatible with LogitsStorage.
                logits = logits.unsqueeze(-1)
            request.py_result.append_context_logits(logits)


def top_k_sampling_batch(logits, top_k=50):
    logits_dim = logits.dim()
    if logits_dim == 1:
        logits = logits.unsqueeze(0)
    # logits should be 2D ：[batch_size, vocab_size]
    batch_size, vocab_size = logits.size()

    raw_probs = torch.softmax(logits, dim=-1)

    # get first top_k logits of each sample and their indices
    values, indices = torch.topk(logits, top_k, dim=-1)
    min_values = values[:, -1].unsqueeze(-1).expand(batch_size, vocab_size)

    # set the logits who is less than first top_k logits to -inf
    logits = torch.where(logits < min_values,
                         torch.full_like(logits, float('-inf')), logits)

    # compute probability distribution
    probs = torch.softmax(logits, dim=-1)

    # sample from the distribution and generate result of [batch_size, 1]
    next_tokens = torch.multinomial(probs, num_samples=1).squeeze(-1)
    token_probs = torch.gather(raw_probs, dim=1,
                               index=next_tokens.unsqueeze(1)).squeeze(-1)
    log_probs = torch.log(token_probs)
    return next_tokens, log_probs


def top_p_sampling_batch(logits, top_p=0.9):
    logits_dim = logits.dim()
    if logits_dim == 1:
        logits = logits.unsqueeze(0)
    # logits should be 2D ：[batch_size, vocab_size]
    batch_size, vocab_size = logits.size()

    raw_probs = torch.softmax(logits, dim=-1)

    # sort the logits of each sample in descending order
    sorted_logits, sorted_indices = torch.sort(logits, descending=True, dim=-1)

    # compute  cumulative probability distribution of each sample
    cumulative_probs = torch.cumsum(torch.softmax(sorted_logits, dim=-1),
                                    dim=-1)

    # get the location of top_p
    sorted_indices_to_remove = cumulative_probs > top_p
    sorted_indices_to_remove[:, 1:] = sorted_indices_to_remove[:, :-1].clone()
    sorted_indices_to_remove[:, 0] = 0

    # set the logits to -inf whose is outside top_p
    indices_to_remove = sorted_indices_to_remove.scatter(
        1, sorted_indices, sorted_indices_to_remove)
    logits = logits.masked_fill(indices_to_remove, float('-inf'))

    # compute probability distribution
    probs = torch.softmax(logits, dim=-1)

    # sample from the distribution and generate result of [batch_size, 1]
    next_tokens = torch.multinomial(probs, num_samples=1).squeeze(-1)
    token_probs = torch.gather(raw_probs, dim=1,
                               index=next_tokens.unsqueeze(1)).squeeze(-1)
    log_probs = torch.log(token_probs)
    return next_tokens, log_probs


def greedy_search_sampling_batch(logits):
    raw_probs = torch.softmax(logits, dim=-1)
    next_tokens = torch.argmax(logits, dim=-1)
    token_probs = torch.gather(raw_probs, dim=1,
                               index=next_tokens.unsqueeze(1)).squeeze(-1)
    log_probs = torch.log(token_probs)
    return next_tokens, log_probs


def decode_single_request(request: LlmRequest, logits):
    assert logits.dim(
    ) == 2 and logits.shape[0] == 1, "logits should have shape [1, vocab_size]"
    if request.sampling_config.top_p is not None and len(
            request.sampling_config.top_p) > 0:
        next_tokens, log_probs = top_p_sampling_batch(
            logits, request.sampling_config.top_p[0])
    elif request.sampling_config.top_k is not None and len(
            request.sampling_config.top_k) > 0:
        next_tokens, log_probs = top_k_sampling_batch(
            logits, request.sampling_config.top_k[0])
    else:
        next_tokens, log_probs = greedy_search_sampling_batch(logits)
    return next_tokens, log_probs


class TorchDecoder(Decoder):

    def __init__(self, max_seq_len: int, mixed_decoder: bool = False):
        self.max_seq_len = max_seq_len
        self.mixed_decoder = mixed_decoder

    def _meet_max_token_stop_criteria(self, request: LlmRequest,
                                      num_tokens: int):
        return (num_tokens - request.py_orig_prompt_len
                >= request.py_max_new_tokens) or (num_tokens
                                                  >= self.max_seq_len)

    def _meet_stop_token_criteria(self, request: LlmRequest):
        if request.py_stop_words_list:
            assert isinstance(
                request.py_stop_words_list,
                list), "request.py_stop_words_list should be a list"
            stop_words_list, prefix_sum = request.py_stop_words_list
            tokens = request.get_tokens(0)
            offset = 0
            for i, offset_end in enumerate(prefix_sum):
                if i > 0:
                    offset = prefix_sum[i - 1]
                stop_word = stop_words_list[offset:offset_end]
                if len(stop_word) > len(tokens):
                    continue
                if tokens[-len(stop_word):] == stop_word:
                    return True
        return False

    def _handle_stop_criteria(self, request: LlmRequest, new_token: int,
                              num_tokens: int, beam_idx: int) -> bool:
        """Handle stop criteria and set appropriate finish reasons and state.
        Returns True if generation should stop."""
        if new_token == request.py_end_id:
            request.state = LlmRequestState.GENERATION_COMPLETE
            request.set_finished_reason(FinishReason.END_ID, beam_idx)
            return True

        if self._meet_max_token_stop_criteria(request, num_tokens):
            request.state = LlmRequestState.GENERATION_COMPLETE
            request.set_finished_reason(FinishReason.LENGTH, beam_idx)
            return True

        if self._meet_stop_token_criteria(request):
            request.state = LlmRequestState.GENERATION_COMPLETE
            request.set_finished_reason(FinishReason.STOP_WORDS, beam_idx)
            return True

        return False

    def update_requests(self, decoder_state: DecoderState) -> None:
        if decoder_state.decoder_event:
            decoder_state.decoder_event.synchronize()
        new_tokens_list = decoder_state.new_tensors_host[
            "new_tokens_host"].tolist()
        scheduled_requests = decoder_state.scheduled_requests

        request_idx = 0
        token_idx = 0
        beam_idx = 0

        def advance_idx(num_tokens=1):
            nonlocal request_idx, token_idx
            request_idx += 1
            token_idx += num_tokens

        def handle_logits(request: LlmRequest, count=1):
            if decoder_state.logits is None:
                return
            if not request.py_return_generation_logits and not request.py_return_log_probs:
                return

            current_slice = slice(token_idx, token_idx + count)
            current_logits = decoder_state.logits[current_slice]

            request.py_result.append_generation_logits(current_logits)

            if not request.py_return_log_probs:
                return

            if decoder_state.log_probs:
                log_probs = decoder_state.log_probs[request_idx]
            else:
                _, log_probs = greedy_search_sampling_batch(current_logits)
            request.py_result.append_log_probs([log_probs.tolist()])

        for request in scheduled_requests.context_requests:
            if request.get_context_remaining_length() != 0:
                advance_idx()
                continue

            if request.state != LlmRequestState.GENERATION_COMPLETE:
                new_token = new_tokens_list[token_idx]
                num_tokens = request.add_new_token(new_token, beam_idx)
                self._handle_stop_criteria(request, new_token, num_tokens,
                                           beam_idx)
                handle_logits(request)
                request.py_decoding_iter += 1
            advance_idx()

        if hasattr(scheduled_requests, 'chunked_requests'):
            request_idx += len(scheduled_requests.chunked_requests)

        extend_requests = []
        generation_requests = []
        for request in scheduled_requests.generation_requests:
            if request.py_draft_tokens is not None:
                extend_requests.append(request)
            else:
                generation_requests.append(request)

        for request in extend_requests:
            if request.state != LlmRequestState.GENERATION_COMPLETE:
                new_token = new_tokens_list[token_idx]
                num_tokens = request.add_new_token(new_token, beam_idx)
                self._handle_stop_criteria(request, new_token, num_tokens,
                                           beam_idx)

                # Accept draft tokens (if we have any) if and only if they match the new
                # token exactly.
                num_accepted = 0
                for draft_token in request.py_draft_tokens:
                    if draft_token != new_token:
                        # Reject.
                        break
                    num_accepted += 1
                    new_token = new_tokens_list[token_idx + num_accepted]
                    num_tokens = request.add_new_token(new_token, beam_idx)

                    if self._handle_stop_criteria(request, new_token,
                                                  num_tokens, beam_idx):
                        break
                handle_logits(request, num_accepted)
                request.py_decoding_iter += 1
                request.py_num_accepted_draft_tokens = num_accepted
                request.py_rewind_len = request.py_draft_pages_allocated - num_accepted
            advance_idx(len(request.py_draft_tokens) + 1)

        for request in generation_requests:
            if request.state != LlmRequestState.GENERATION_COMPLETE:
                new_token = new_tokens_list[token_idx]
                num_tokens = request.add_new_token(new_token, beam_idx)
                self._handle_stop_criteria(request, new_token, num_tokens,
                                           beam_idx)
                handle_logits(request)
                request.py_decoding_iter += 1
            advance_idx()

    def _mixed_decode(self, scheduled_requests: ScheduledRequests,
                      model_outputs) -> DecoderState:
        logits = model_outputs["logits"]

        state = DecoderState(
            scheduled_requests=scheduled_requests,
            logits=logits,
            log_probs=[],
        )

        new_tokens_device_array = []

        idx = 0

        for request in scheduled_requests.context_requests:
            assert not request.py_return_context_logits, "Return context logits not supported"
            token_logits = logits[idx:idx + 1, :]
            new_token, log_probs = decode_single_request(request, token_logits)
            new_tokens_device_array.append(new_token)
            log_probs = [log_probs.tolist()
                         ] if request.py_return_log_probs else None
            state.log_probs.append(log_probs)  # Currently always beam_width=1
            idx += 1

        for request in scheduled_requests.generation_requests:
            if request.state == LlmRequestState.GENERATION_COMPLETE:
                continue
            assert request.py_draft_tokens is None, "Speculative decoding not supported in SeparateDecoder."
            token_logits = logits[idx:idx + 1, :]
            new_token, log_probs = decode_single_request(request, token_logits)
            new_tokens_device_array.append(new_token)
            log_probs = [log_probs.tolist()
                         ] if request.py_return_log_probs else None
            state.log_probs.append(log_probs)  # Currently always beam_width=1
            idx += 1

        new_tokens_device = torch.cat(new_tokens_device_array)
        new_tokens_host = new_tokens_device.to('cpu', non_blocking=True)
        state.new_tensors_device = {"new_tokens_device": new_tokens_device}
        state.new_tensors_host = {"new_tokens_host": new_tokens_host}
        state.decoder_event = torch.cuda.Event()
        state.decoder_event.record()

        return state

    def _batch_decode(self, scheduled_requests: ScheduledRequests,
                      model_outputs) -> DecoderState:
        logits = model_outputs["logits"]
        new_tokens_device = torch.argmax(logits, dim=-1)
        new_tokens_host = new_tokens_device.to('cpu', non_blocking=True)
        decoder_event = torch.cuda.Event()
        decoder_event.record()
        return DecoderState(
            scheduled_requests=scheduled_requests,
            logits=logits,
            new_tensors_device={"new_tokens_device": new_tokens_device},
            new_tensors_host={"new_tokens_host": new_tokens_host},
            decoder_event=decoder_event)

    def decode_async(self, scheduled_requests: ScheduledRequests,
                     model_outputs) -> DecoderState:
        if self.mixed_decoder:
            return self._mixed_decode(scheduled_requests, model_outputs)
        else:
            return self._batch_decode(scheduled_requests, model_outputs)


class TorchStarAttentionDecoder(TorchDecoder):

    def update_one_request(self, request: LlmRequest,
                           new_tokens_list: list[int], logits: torch.Tensor):
        beam_idx = 0

        output_token_idx = request.output_token_idx
        new_token = new_tokens_list[output_token_idx]
        num_tokens = request.add_new_token(new_token, beam_idx)

        current_logits = logits[output_token_idx].unsqueeze(0)
        request.py_result.append_generation_logits(current_logits)
        if request.py_return_log_probs:
            _, log_probs = greedy_search_sampling_batch(current_logits)
            request.py_result.append_log_probs([log_probs.tolist()])

        self._handle_stop_criteria(request, new_token, num_tokens, beam_idx)
        if request.state != LlmRequestState.GENERATION_COMPLETE:
            request.py_decoding_iter += 1

    def update_requests(self, decoder_state: DecoderState):
        if decoder_state.decoder_event:
            decoder_state.decoder_event.synchronize()
        new_tokens_list = decoder_state.new_tensors_host[
            "new_tokens_host"].tolist()
        logits = decoder_state.logits

        for request in decoder_state.scheduled_requests.context_requests:
            if request.state == LlmRequestState.GENERATION_IN_PROGRESS:
                self.update_one_request(request, new_tokens_list, logits)

        for request in decoder_state.scheduled_requests.generation_requests:
            self.update_one_request(request, new_tokens_list, logits)


class Algorithms:

    def defined_algorithms(self):
        return [attr for attr in dir(self) if not attr.startswith("__")]

    def __repr__(self):
        algs = self.defined_algorithms()
        return f"Algs({', '.join(algs)})"


class TRTLLMDecoder(Decoder):

    def __init__(
        self,
        executor_config: ExecutorConfig,
        model,
        model_dtype,
        mapping: Mapping,
        decoding_mode: DecodingMode,
    ):

        vocab_size = model.config.vocab_size
        num_hidden_layers = model.config.num_hidden_layers
        hidden_size = model.config.hidden_size
        num_heads = model.config.num_attention_heads

        self.model_datatype = torch_dtype_to_binding(model_dtype)
        self.logits_datatype = DataType.FLOAT
        self.decoding_mode = decoding_mode
        self.executor_config = executor_config
        self.decoding_config = self.executor_config.decoding_config if self.executor_config.decoding_config else DecodingConfig(
            decoding_mode)
        max_attn_window = self.executor_config.kv_cache_config.max_attention_window
        self.max_attention_window = max_attn_window if max_attn_window is not None else executor_config.max_seq_len
        self.max_num_sequences = mapping.pp_size * self.executor_config.max_batch_size
        self.max_seq_idle_microseconds = 180 * 1000 * 1000
        self.max_decoding_tokens = 1  # It must be 1 when not in speculative decoding

        self.world_config = WorldConfig.mpi(mapping.gpus_per_node,
                                            mapping.tp_size, mapping.pp_size)
        self.model_config = ModelConfig(vocab_size, num_hidden_layers,
                                        num_hidden_layers, 0, num_heads,
                                        hidden_size, self.model_datatype)

        self._initialize_store()
        self._instantiate_algorithms()

    def _initialize_store(self):
        torch_stream = torch.cuda.current_stream()
        cuda_stream = CudaStream(torch_stream.cuda_stream)
        buffer_manager = BufferManager(stream=cuda_stream)

        self.store = {
            "torch_stream":
            torch_stream,
            "cuda_stream":
            cuda_stream,
            "buffer_manager":
            buffer_manager,
            "decoder_buffers":
            DecoderBuffers(self.max_num_sequences,
                           self.executor_config.max_beam_width,
                           self.max_attention_window, self.max_decoding_tokens,
                           buffer_manager, self.model_config,
                           self.world_config),
            "decoder_input_buffers":
            DecoderInputBuffers(self.executor_config.max_batch_size,
                                self.max_decoding_tokens, buffer_manager),
            "new_tokens_device_tensor":
            torch.empty((
                self.executor_config.max_batch_size,
                self.executor_config.max_beam_width,
            ),
                        dtype=torch.int,
                        device='cuda'),
            "sequence_lengths_host":
            torch.empty((
                self.executor_config.max_batch_size,
                self.executor_config.max_beam_width,
            ),
                        dtype=torch.int)
        }

    def _instantiate_algorithms(self):
        self.algs = Algorithms()
        self.algs.decoder = GptDecoderBatched(
            stream=self.store["cuda_stream"],
            speculative_decoding_mode=SpeculativeDecodingMode.NoneType(),
            dtype=self.logits_datatype)
        self.algs.decoder.setup(
            mode=self.decoding_mode,
            max_batch_size=self.executor_config.max_batch_size,
            max_beam_width=self.executor_config.max_beam_width,
            max_attention_window=self.max_attention_window,
            sink_token_length=0,
            max_sequence_length=self.executor_config.max_seq_len,
            max_tokens_per_step=self.max_decoding_tokens,
            dtype=self.logits_datatype,
            model_config=self.model_config,
            world_config=self.world_config)
        self.algs.generate_request_options = GenerateRequestOptions(
            speculative_decoding_fast_logits=False,
            is_leader_in_orch_mode=False,
            is_normalize_log_probs=False)
        self.algs.create_new_decoder_requests = CreateNewDecoderRequests()
        self.algs.handle_context_logits = HandleContextLogits()
        self.algs.handle_generation_logits = HandleGenerationLogits()
        self.algs.make_decoding_batch_input_output = MakeDecodingBatchInputOutput(
        )

    def setup_decoder_step(self, requests):
        batch_slots, decoder_requests, sampling_configs = self.algs.generate_request_options(
            self.model_config, self.world_config, self.decoding_config,
            requests, self.store["buffer_manager"], self.logits_datatype,
            self.store["decoder_input_buffers"])

        if len(decoder_requests):
            self.algs.create_new_decoder_requests(
                batch_slots, decoder_requests, sampling_configs,
                self.model_config, self.algs.decoder, self.store["cuda_stream"],
                self.executor_config.max_seq_len)

            local_batch_size = len(batch_slots)
            sampling_config = make_sampling_config(sampling_configs)
            self.algs.decoder.underlying_decoder().setup(
                sampling_config, local_batch_size, batch_slots,
                self.algs.decoder.decoder_state.joint_decoding_output,
                decoder_requests)

    def decode_async(self, scheduled_requests: ScheduledRequests,
                     model_outputs):
        self.batch_size = scheduled_requests.batch_size
        for req in itertools.chain(scheduled_requests.context_requests,
                                   scheduled_requests.generation_requests):
            self.beam_width = req.sampling_config.beam_width
            break

        logits = model_outputs["logits"].reshape(
            (self.batch_size, self.beam_width, -1))

        self.setup_decoder_step(scheduled_requests.context_requests)

        # Note: In runtimeBuffers.cpp, num_context_logits is set to:
        #       numContextLogits.at(batchIdx) = modelConfig.computeContextLogits() ? contextChunkSize : 1;
        # Revisit this when we support chunked context.
        num_context_logits = [1] * self.batch_size
        logits_index = self.algs.handle_context_logits(
            scheduled_requests.context_requests, num_context_logits, logits,
            self.store["decoder_buffers"], self.model_config,
            self.store["buffer_manager"], self.store["cuda_stream"])

        self.algs.handle_generation_logits(
            logits_index, scheduled_requests.generation_requests,
            self.store["decoder_buffers"], self.model_config,
            self.store["buffer_manager"], logits)

        decoding_input, self.decoding_output = self.algs.make_decoding_batch_input_output(
            scheduled_requests.context_requests,
            scheduled_requests.generation_requests,
            self.store["decoder_buffers"], self.store["decoder_input_buffers"],
            self.algs.decoder.decoder_state, self.model_config,
            self.max_num_sequences, self.beam_width,
            self.store["buffer_manager"], self.store["cuda_stream"])

        self.algs.decoder.forward_async(self.decoding_output, decoding_input)

        # NOTE: The following code prepares a new_tokens_device_tensor in accordance with the
        #       current implementation of model_engine.
        # TODO: When we support speculative decoding:
        # new_tokens_device_tensor should be, for speculative decoding cases: [batch, 1 + draft_len], others: [batch]
        new_tokens_device_tensor = self.store[
            "new_tokens_device_tensor"][:self.batch_size, :self.beam_width]
        seq_slots = [
            request.seq_slot for request in itertools.chain(
                scheduled_requests.context_requests,
                scheduled_requests.generation_requests)
        ]
        new_tokens_device_tensor.copy_(
            self.algs.decoder.decoder_state.all_new_tokens[0][seq_slots],
            non_blocking=True)
        new_tokens_device_tensor = new_tokens_device_tensor.view(-1)

        # NOTE: If we overwrite seq lens on every iteration then overlap scheduling seemingly works.
        #       This could be a race condition.
        self.store["sequence_lengths_host"].copy_(
            self.algs.decoder.decoder_state.sequence_lengths, non_blocking=True)

        # TODO: We should instead copy on every iteration, however this doesn't work for overlap scheduling atm.
        #       It's still not understood why.
        # sequence_lengths = self.store["decoder_buffers"].sequence_lengths.to('cpu', non_blocking=True)

        new_output_tokens = self.algs.decoder.decoder_state.all_new_tokens.to(
            'cpu', non_blocking=True)
        finished_sum = self.algs.decoder.decoder_state.finished_sum.to(
            'cpu', non_blocking=True)
        finish_reasons = self.algs.decoder.decoder_state.finish_reasons.to(
            'cpu', non_blocking=True)

        new_tensors_device = {"new_tokens_device": new_tokens_device_tensor}

        new_tensors_host = {
            "new_tokens_host": new_output_tokens,
            "finished_sum_host": finished_sum,
            "finish_reasons_host": finish_reasons,
            "sequence_lengths_host": self.store["sequence_lengths_host"]
        }

        decoder_event = torch.cuda.Event()
        decoder_event.record()

        return DecoderState(scheduled_requests=scheduled_requests,
                            logits=logits,
                            new_tensors_device=new_tensors_device,
                            new_tensors_host=new_tensors_host,
                            decoder_event=decoder_event)

    def update_requests(self, decoder_state: DecoderState):
        scheduled_requests = decoder_state.scheduled_requests
        new_tensors_host = decoder_state.new_tensors_host
        decoder_event = decoder_state.decoder_event

        if decoder_event:
            decoder_event.synchronize()

        new_tokens_host = new_tensors_host["new_tokens_host"]
        finished_sum_host = new_tensors_host["finished_sum_host"]
        finish_reasons_host = new_tensors_host["finish_reasons_host"]
        sequence_lengths_host_data = new_tensors_host["sequence_lengths_host"]

        for request in itertools.chain(scheduled_requests.context_requests,
                                       scheduled_requests.generation_requests):
            if request.is_context_init_state:
                continue

            seq_slot = request.seq_slot
            num_generated_tokens = request.num_draft_tokens + 1
            current_num_of_tokens = request.max_beam_num_tokens

            num_new_tokens = [0] * self.beam_width
            num_dropped_tokens = [0] * self.beam_width

            for beam in range(self.beam_width):
                seq_len = sequence_lengths_host_data[seq_slot * self.beam_width
                                                     + beam].item()
                num_new_tokens[beam] = min(
                    num_generated_tokens,
                    seq_len - request.get_num_tokens(beam))
                num_dropped_tokens[
                    beam] = num_generated_tokens - num_new_tokens[beam]

                for step in range(num_new_tokens[beam]):
                    new_token = new_tokens_host[step][seq_slot][beam]
                    request.add_new_token(new_token, beam)

                finish_reason = finish_reasons_host[seq_slot * self.beam_width +
                                                    beam].item()
                request.set_finished_reason(FinishReason(finish_reason), beam)

            # Set number of tokens predicted per runtime iteration. Will be > 1 for speculative decoding.
            request.update_num_tokens_per_iteration(
                request.max_beam_num_tokens - current_num_of_tokens,
                self.model_config)

            # Increment the decoding iteration counter
            if request.state != LlmRequestState.GENERATION_COMPLETE:
                request.py_decoding_iter += 1

            if finished_sum_host[seq_slot] == self.beam_width:
                request.state = LlmRequestState.GENERATION_COMPLETE
