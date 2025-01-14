# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the terms described in the LICENSE file in
# the root directory of this source tree.

from typing import AsyncGenerator

from fireworks.client import Fireworks
from llama_models.datatypes import CoreModelId

from llama_models.llama3.api.chat_format import ChatFormat
from llama_models.llama3.api.datatypes import Message
from llama_models.llama3.api.tokenizer import Tokenizer
from llama_stack.apis.inference import *  # noqa: F403
from llama_stack.distribution.request_headers import NeedsRequestProviderData
from llama_stack.providers.utils.inference.model_registry import (
    build_model_alias,
    ModelRegistryHelper,
)
from llama_stack.providers.utils.inference.openai_compat import (
    get_sampling_options,
    process_chat_completion_response,
    process_chat_completion_stream_response,
    process_completion_response,
    process_completion_stream_response,
)
from llama_stack.providers.utils.inference.prompt_adapter import (
    chat_completion_request_to_prompt,
    completion_request_to_prompt,
    convert_message_to_dict,
    request_has_media,
)

from .config import FireworksImplConfig


model_aliases = [
    build_model_alias(
        "fireworks/llama-v3p1-8b-instruct",
        CoreModelId.llama3_1_8b_instruct.value,
    ),
    build_model_alias(
        "fireworks/llama-v3p1-70b-instruct",
        CoreModelId.llama3_1_70b_instruct.value,
    ),
    build_model_alias(
        "fireworks/llama-v3p1-405b-instruct",
        CoreModelId.llama3_1_405b_instruct.value,
    ),
    build_model_alias(
        "fireworks/llama-v3p2-1b-instruct",
        CoreModelId.llama3_2_3b_instruct.value,
    ),
    build_model_alias(
        "fireworks/llama-v3p2-3b-instruct",
        CoreModelId.llama3_2_11b_vision_instruct.value,
    ),
    build_model_alias(
        "fireworks/llama-v3p2-11b-vision-instruct",
        CoreModelId.llama3_2_11b_vision_instruct.value,
    ),
    build_model_alias(
        "fireworks/llama-v3p2-90b-vision-instruct",
        CoreModelId.llama3_2_90b_vision_instruct.value,
    ),
    build_model_alias(
        "fireworks/llama-guard-3-8b",
        CoreModelId.llama_guard_3_8b.value,
    ),
    build_model_alias(
        "fireworks/llama-guard-3-11b-vision",
        CoreModelId.llama_guard_3_11b_vision.value,
    ),
]


class FireworksInferenceAdapter(
    ModelRegistryHelper, Inference, NeedsRequestProviderData
):
    def __init__(self, config: FireworksImplConfig) -> None:
        ModelRegistryHelper.__init__(self, model_aliases)
        self.config = config
        self.formatter = ChatFormat(Tokenizer.get_instance())

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    def _get_client(self) -> Fireworks:
        fireworks_api_key = None
        if self.config.api_key is not None:
            fireworks_api_key = self.config.api_key
        else:
            provider_data = self.get_request_provider_data()
            if provider_data is None or not provider_data.fireworks_api_key:
                raise ValueError(
                    'Pass Fireworks API Key in the header X-LlamaStack-ProviderData as { "fireworks_api_key": <your api key>}'
                )
            fireworks_api_key = provider_data.fireworks_api_key
        return Fireworks(api_key=fireworks_api_key)

    async def completion(
        self,
        model_id: str,
        content: InterleavedTextMedia,
        sampling_params: Optional[SamplingParams] = SamplingParams(),
        response_format: Optional[ResponseFormat] = None,
        stream: Optional[bool] = False,
        logprobs: Optional[LogProbConfig] = None,
    ) -> AsyncGenerator:
        model = await self.model_store.get_model(model_id)
        request = CompletionRequest(
            model=model.provider_resource_id,
            content=content,
            sampling_params=sampling_params,
            response_format=response_format,
            stream=stream,
            logprobs=logprobs,
        )
        if stream:
            return self._stream_completion(request)
        else:
            return await self._nonstream_completion(request)

    async def _nonstream_completion(
        self, request: CompletionRequest
    ) -> CompletionResponse:
        params = await self._get_params(request)
        r = await self._get_client().completion.acreate(**params)
        return process_completion_response(r, self.formatter)

    async def _stream_completion(self, request: CompletionRequest) -> AsyncGenerator:
        params = await self._get_params(request)

        # Wrapper for async generator similar
        async def _to_async_generator():
            stream = self._get_client().completion.create(**params)
            for chunk in stream:
                yield chunk

        stream = _to_async_generator()
        async for chunk in process_completion_stream_response(stream, self.formatter):
            yield chunk

    def _build_options(
        self, sampling_params: Optional[SamplingParams], fmt: ResponseFormat
    ) -> dict:
        options = get_sampling_options(sampling_params)
        options.setdefault("max_tokens", 512)

        if fmt:
            if fmt.type == ResponseFormatType.json_schema.value:
                options["response_format"] = {
                    "type": "json_object",
                    "schema": fmt.json_schema,
                }
            elif fmt.type == ResponseFormatType.grammar.value:
                options["response_format"] = {
                    "type": "grammar",
                    "grammar": fmt.bnf,
                }
            else:
                raise ValueError(f"Unknown response format {fmt.type}")

        return options

    async def chat_completion(
        self,
        model_id: str,
        messages: List[Message],
        sampling_params: Optional[SamplingParams] = SamplingParams(),
        tools: Optional[List[ToolDefinition]] = None,
        tool_choice: Optional[ToolChoice] = ToolChoice.auto,
        tool_prompt_format: Optional[ToolPromptFormat] = ToolPromptFormat.json,
        response_format: Optional[ResponseFormat] = None,
        stream: Optional[bool] = False,
        logprobs: Optional[LogProbConfig] = None,
    ) -> AsyncGenerator:
        model = await self.model_store.get_model(model_id)
        request = ChatCompletionRequest(
            model=model.provider_resource_id,
            messages=messages,
            sampling_params=sampling_params,
            tools=tools or [],
            tool_choice=tool_choice,
            tool_prompt_format=tool_prompt_format,
            response_format=response_format,
            stream=stream,
            logprobs=logprobs,
        )

        if stream:
            return self._stream_chat_completion(request)
        else:
            return await self._nonstream_chat_completion(request)

    async def _nonstream_chat_completion(
        self, request: ChatCompletionRequest
    ) -> ChatCompletionResponse:
        params = await self._get_params(request)
        if "messages" in params:
            r = await self._get_client().chat.completions.acreate(**params)
        else:
            r = await self._get_client().completion.acreate(**params)
        return process_chat_completion_response(r, self.formatter)

    async def _stream_chat_completion(
        self, request: ChatCompletionRequest
    ) -> AsyncGenerator:
        params = await self._get_params(request)

        async def _to_async_generator():
            if "messages" in params:
                stream = await self._get_client().chat.completions.acreate(**params)
            else:
                stream = self._get_client().completion.create(**params)
            for chunk in stream:
                yield chunk

        stream = _to_async_generator()
        async for chunk in process_chat_completion_stream_response(
            stream, self.formatter
        ):
            yield chunk

    async def _get_params(
        self, request: Union[ChatCompletionRequest, CompletionRequest]
    ) -> dict:
        input_dict = {}
        media_present = request_has_media(request)

        if isinstance(request, ChatCompletionRequest):
            if media_present:
                input_dict["messages"] = [
                    await convert_message_to_dict(m) for m in request.messages
                ]
            else:
                input_dict["prompt"] = chat_completion_request_to_prompt(
                    request, self.get_llama_model(request.model), self.formatter
                )
        else:
            assert (
                not media_present
            ), "Fireworks does not support media for Completion requests"
            input_dict["prompt"] = completion_request_to_prompt(request, self.formatter)

        # Fireworks always prepends with BOS
        if "prompt" in input_dict:
            if input_dict["prompt"].startswith("<|begin_of_text|>"):
                input_dict["prompt"] = input_dict["prompt"][len("<|begin_of_text|>") :]

        return {
            "model": request.model,
            **input_dict,
            "stream": request.stream,
            **self._build_options(request.sampling_params, request.response_format),
        }

    async def embeddings(
        self,
        model_id: str,
        contents: List[InterleavedTextMedia],
    ) -> EmbeddingsResponse:
        raise NotImplementedError()
