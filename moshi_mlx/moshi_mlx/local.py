# Copyright (c) Kyutai, all rights reserved.
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

import argparse
import asyncio
import json
from logging import WARN
from os.path import exists
import queue
import os
import tarfile
import time
import numpy as np
import multiprocessing
from pathlib import Path
import sentencepiece
from enum import Enum
import typing as tp
import sphn
import aiohttp
from aiohttp import web

import mlx.core as mx
import mlx.nn as nn

import rustymimi
import moshi_mlx

import huggingface_hub

SAMPLE_RATE = 24000
FRAME_SIZE = 1920
CHANNELS = 1

def colorize(text, color):
    code = f"\033[{color}m"
    restore = "\033[0m"
    return "".join([code, text, restore])

def log(level: str, msg: str):
    if level == "warning":
        prefix = colorize("[Warn]", "1;31")
    elif level == "info":
        prefix = colorize("[Info]", "1;34")
    elif level == "error":
        prefix = colorize("[Err ]", "1;31")
    else:
        raise ValueError(f"Unknown level {level}")
    print(prefix + " " + msg)


def hf_hub_download(repo, path: str) -> str:
    if repo is None or repo == "":
        raise ValueError(f"the --hf-repo flag is required to retrieve {path}")
    return huggingface_hub.hf_hub_download(repo, path)

class Stats:
    send_times: tp.List[float] = []
    model_times: tp.List[tp.Tuple[float, float]] = []
    recv_times: tp.List[float] = []


class PrinterType(Enum):
    TOKEN = 1
    PENDING = 2
    INFO = 3
    WARNING = 4
    ERROR = 5
    LAG = 6
    HEADER = 7
    EVENT = 8
    QSIZE = 9


def full_warmup(audio_tokenizer, client_to_server, server_to_client):
    for i in range(4):
        pcm_data = np.array([0.0] * 1920).astype(np.float32)
        audio_tokenizer.encode(pcm_data)
        while True:
            time.sleep(0.01)
            data = audio_tokenizer.get_encoded()
            if data is not None:
                break
        client_to_server.put_nowait(data)
        if i == 0:
            continue
        audio_tokens = server_to_client.get()
        audio_tokenizer.decode(audio_tokens)
        while True:
            time.sleep(0.01)
            data = audio_tokenizer.get_decoded()
            if data is not None:
                break


def model_server(client_to_server, server_to_client, args):
    model_file = args.model
    tokenizer_file = args.tokenizer
    if model_file is None:
        if args.quantized == 8:
            model_file = hf_hub_download(
                args.hf_repo, "moshiko_mlx_301e30bf@120.q8.safetensors"
            )
        elif args.quantized == 4:
            model_file = hf_hub_download(
                args.hf_repo, "moshiko_mlx_301e30bf@120.q4.safetensors"
            )
        elif args.quantized is not None:
            raise ValueError(f"Invalid quantized value: {args.quantized}")
        else:
            model_file = hf_hub_download(
                args.hf_repo, "moshiko_mlx_301e30bf@120.safetensors"
            )
    if tokenizer_file is None:
        tokenizer_file = hf_hub_download(args.hf_repo, "tokenizer_spm_32k_3.model")
    steps = args.steps

    log("info", f"[SERVER] loading text tokenizer {tokenizer_file}")
    text_tokenizer = sentencepiece.SentencePieceProcessor(tokenizer_file)
    mx.random.seed(299792458)
    lm_config = moshi_mlx.models.config_v0_1()
    model = moshi_mlx.models.Lm(lm_config)
    model.set_dtype(mx.bfloat16)
    if args.quantized is not None:
        group_size = 32 if args.quantized == 4 else 64
        nn.quantize(model, bits=args.quantized, group_size=group_size)

    log("info", f"[SERVER] loading weights {model_file}")
    model.load_weights(model_file, strict=True)
    log("info", "[SERVER] weights loaded")

    model.warmup()
    log("info", "[SERVER] model warmed up")
    gen = moshi_mlx.models.LmGen(
        model=model,
        max_steps=steps + 5,
        text_sampler=moshi_mlx.utils.Sampler(),
        audio_sampler=moshi_mlx.utils.Sampler(),
        check=False,
    )

    server_to_client.put("start")
    log("info", "[SERVER] connected!")
    try:
        while True:
            data = client_to_server.get()
            data = mx.array(data).transpose(1, 0)[:, :8]
            text_token = gen.step(data)
            text_token = text_token[0].item()
            audio_tokens = gen.last_audio_tokens()
            if text_token not in (0, 3):
                _text = text_tokenizer.id_to_piece(text_token)
                _text = _text.replace("▁", " ")
                log("info", f"token {_text}")
            if audio_tokens is not None:
                audio_tokens = np.array(audio_tokens).astype(np.uint32)
                server_to_client.put_nowait(audio_tokens)
    except KeyboardInterrupt:
        pass


def web_server(client_to_server, server_to_client, args):
    mimi_file = args.mimi
    if mimi_file is None:
        mimi_file = hf_hub_download(
            args.hf_repo, "tokenizer-e351c8d8-checkpoint125.safetensors"
        )
    input_queue = queue.Queue()
    output_queue = queue.Queue()
    audio_tokenizer = rustymimi.StreamTokenizer(mimi_file)
    start = server_to_client.get()
    log("info", f"[CLIENT] received '{start}' from server, starting...")

    full_warmup(audio_tokenizer, client_to_server, server_to_client)

    async def send_loop():
        while True:
            await asyncio.sleep(0.001)
            try:
                pcm_data = input_queue.get(block=False)
                audio_tokenizer.encode(pcm_data)
            except queue.Empty:
                continue

    async def recv_loop():
        while True:
            data = audio_tokenizer.get_decoded()
            if data is None:
                await asyncio.sleep(0.001)
                continue
            output_queue.put_nowait(data)

    async def send_loop2():
        while True:
            data = audio_tokenizer.get_encoded()
            if data is None:
                await asyncio.sleep(0.001)
                continue
            client_to_server.put_nowait(data)

    async def recv_loop2():
        while True:
            try:
                audio_tokens = server_to_client.get(block=False)
            except queue.Empty:
                await asyncio.sleep(0.001)
                continue
            audio_tokenizer.decode(audio_tokens)

    lock = asyncio.Lock()
    async def handle_chat(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async def recv_loop():
            nonlocal close
            try:
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.ERROR:
                        log("error", f"{ws.exception()}")
                        break
                    elif message.type == aiohttp.WSMsgType.CLOSED:
                        break
                    elif message.type != aiohttp.WSMsgType.BINARY:
                        log("error", f"unexpected message type {message.type}")
                        continue
                    message = message.data
                    if not isinstance(message, bytes):
                        log("error", f"unsupported message type {type(message)}")
                        continue
                    if len(message) == 0:
                        log("warning", "empty message")
                        continue
                    kind = message[0]
                    if kind == 1:  # audio
                        payload = message[1:]
                        opus_reader.append_bytes(payload)
                    else:
                        log("warning", f"unknown message kind {kind}")
            finally:
                close = True
                log("info", "connection closed")

        async def opus_loop():
            all_pcm_data = None

            while True:
                if close:
                    return
                await asyncio.sleep(0.001)
                pcm = opus_reader.read_pcm()
                if pcm.shape[-1] == 0:
                    continue
                if all_pcm_data is None:
                    all_pcm_data = pcm
                else:
                    all_pcm_data = np.concatenate((all_pcm_data, pcm))
                while all_pcm_data.shape[-1] >= FRAME_SIZE:
                    chunk = all_pcm_data[: FRAME_SIZE]
                    all_pcm_data = all_pcm_data[FRAME_SIZE :]
                    input_queue.put_nowait(chunk)

        async def send_loop():
            while True:
                if close:
                    return
                await asyncio.sleep(0.001)
                msg = opus_writer.read_bytes()
                if len(msg) > 0:
                    await ws.send_bytes(b"\x01" + msg)

        async def another_loop():
            while True:
                if close:
                    return
                await asyncio.sleep(0.001)
                try:
                    pcm_data = output_queue.get(block=False)
                    assert pcm_data.shape == (1920,), pcm_data.shape
                    opus_writer.append_pcm(pcm_data)
                except queue.Empty:
                    continue

        log("info", "accepted connection")
        close = False
        async with lock:
            opus_writer = sphn.OpusStreamWriter(SAMPLE_RATE)
            opus_reader = sphn.OpusStreamReader(SAMPLE_RATE)
            # Send the handshake.
            await ws.send_bytes(b'\x00')
            await asyncio.gather(opus_loop(), recv_loop(), send_loop(), another_loop())
        log("info", "done with connection")
        return ws


    async def go():
        app = web.Application()
        app.router.add_get('/api/chat', handle_chat)
        static_path: None | str = None
        if args.static is None:
            log("info", f"retrieving the static content")
            dist_tgz = hf_hub_download(args.hf_repo, "dist.tgz")
            dist_tgz = Path(dist_tgz)
            dist = dist_tgz.parent / "dist"
            if not dist.exists():
                with tarfile.open(dist_tgz, 'r:gz') as tar:
                    tar.extractall(path=dist_tgz.parent)
            static_path = str(dist)
        elif args.static != "none":
            # When set to the "none" string, we don't serve any static content.
            static_path = args.static
        if static_path is not None:
            async def handle_root(_):
                return web.FileResponse(os.path.join(static_path, 'index.html'))
            log("info", f"serving static content from {static_path}")
            app.router.add_get('/', handle_root)
            app.router.add_static('/', path=static_path, name='static')
        log("info", f"listening to ws://{args.host}:{args.port}")
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, args.host, args.port)
        await asyncio.gather(recv_loop(), send_loop(), recv_loop2(), send_loop2(), site.start())
        await runner.cleanup()

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tokenizer", type=str)
    parser.add_argument("--model", type=str)
    parser.add_argument("--mimi", type=str)
    parser.add_argument("--quantized", type=int)
    parser.add_argument("--steps", default=2500, type=int)
    parser.add_argument("--hf-repo", type=str, default="")
    parser.add_argument("--static", type=str)
    parser.add_argument("--host", default="localhost", type=str)
    parser.add_argument("--port", default=8998, type=int)

    args = parser.parse_args()

    client_to_server = multiprocessing.Queue()
    server_to_client = multiprocessing.Queue()

    # Create two processes
    subprocess_args = client_to_server, server_to_client, args
    p1 = multiprocessing.Process(target=web_server, args=subprocess_args)
    p2 = multiprocessing.Process(target=model_server, args=subprocess_args)

    # Start the processes
    p1.start()
    p2.start()
    events = []

    try:
        while p1.is_alive() and p2.is_alive():
            time.sleep(0.001)
    except KeyboardInterrupt:
        log("warning", "Interrupting, exiting connection.")
        p1.terminate()
        p2.terminate()

    # Wait for both processes to finish
    p1.join()
    p2.join()
    log("info", "All done!")


if __name__ == "__main__":
    main()
