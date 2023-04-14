"""This model provide a ChatGPT-compatible Restful server api for chat completion.

Usage:

python3 -m fastchat.serve.api

Reference: https://platform.openai.com/docs/api-reference/chat/create
"""

from typing import Union, Dict, List, Optional, Any

import argparse
import json
import logging
import time

import fastapi
import httpx
import pydantic
import shortuuid
import uvicorn

from fastchat.conversation import get_default_conv_template, SeparatorStyle

logger = logging.getLogger(__name__)

app = fastapi.FastAPI()
controller_url = None
headers = {"User-Agent": "FastChat API Server"}


class CompletionRequest(pydantic.BaseModel):
    # TODO: support streaming, stop with a list of text etc.
    model: str
    messages: List[Dict[str, str]]
    temperature: Optional[float] = 0.7
    n: int = 1
    max_tokens: Optional[int] = None
    stop: Optional[str] = None


@app.post("/v1/chat/completions")
async def create_chat_completion(request: CompletionRequest):
    """Creates a completion for the chat message"""
    payload, skip_echo_len = generate_payload(
        request.model,
        request.messages,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
        stop=request.stop)

    choices = []
    # TODO: batch the requests. maybe not necessary if using CacheFlow worker
    for i in range(request.n):
        content = await chat_completion(request.model, payload, skip_echo_len)
        choices.append({
            "index": i,
            "message": {
                "role": "assistant",
                "content": content,
            },
            # TODO: support other finish_reason
            "finish_reason": "stop"
        })

    return {
        "id": shortuuid.random(),
        "object": "chat.completion",
        "created": int(time.time()),
        "choices": choices,
        # TODO: support usage field
        # "usage": {
        #     "prompt_tokens": 9,
        #     "completion_tokens": 12,
        #     "total_tokens": 21
        # }
    }



def generate_payload(model_name: str, messages: List[Dict[str, str]],
                     *, temperature: float, max_tokens: int, stop: Union[str, None]):
    is_chatglm = "chatglm" in model_name.lower()
    conv = get_default_conv_template(model_name)

    for message in messages:
        msg_role = message["role"]
        if msg_role == "system":
            conv.system = message["content"]
        elif msg_role == "user":
            conv.append_message(conv.roles[0], message["content"])
        elif msg_role == "assistant":
            conv.append_message(conv.roles[1], message["content"])
        else:
            raise ValueError(f"Unknown role: {msg_role}")

    if is_chatglm:
        prompt = conv.messages[conv.offset:]
        skip_echo_len = len(conv.messages[-2][1]) + 1
    else:
        prompt = conv.get_prompt()
        skip_echo_len = len(prompt.replace("</s>", " ")) + 1

    if stop is None:
        stop = conv.sep if conv.sep_style == SeparatorStyle.SINGLE else conv.sep2

    payload = {
        "model": model_name,
        "prompt": prompt,
        "temperature": temperature,
        "max_new_tokens": max_tokens,
        "stop": stop,
    }

    logger.debug(f"==== request ====\n{payload}")
    return payload, skip_echo_len


async def chat_completion(model_name: str, payload: Dict[str, Any], skip_echo_len: int):
    async with httpx.AsyncClient() as client:
        ret = await client.post(controller_url + "/get_worker_address", json={"model": model_name})
        worker_addr = ret.json()["address"]
        # No available worker
        if worker_addr == "":
            raise ValueError(f"No available worker for {model_name}")

        logger.debug(f"model_name: {model_name}, worker_addr: {worker_addr}")

        output = ""
        async with client.stream("POST", worker_addr + "/worker_generate_stream",
                                 headers=headers, json=payload, timeout=20) as response:
            async for chunk in response.aiter_lines(decode_unicode=False, delimiter=b"\0"):
                if chunk:
                    data = json.loads(chunk.decode())
                    if data["error_code"] == 0:
                        output = data["text"][skip_echo_len:].strip()
        return output
    

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FastChat ChatGPT-compatible Restful API server.")
    parser.add_argument("--host", type=str, default="localhost", help="host name")
    parser.add_argument("--port", type=int, default=8000, help="port number")
    parser.add_argument("--controller-address", type=str, default="http://localhost:21001",
        help="The address of the model controller.")
 
    args = parser.parse_args()
    controller_url = args.controller_address

    uvicorn.run("api:app", host=args.host, port=args.port, reload=True)