import asyncio
import json
import os
import ssl
import sys
import urllib.parse
from time import time
from typing import Generator
from typing import List
from typing import Union
import aiohttp
from websockets.client import connect, WebSocketClientProtocol
import certifi
import httpx
from BingImageCreator import ImageGenAsync
from .ip_rand import get_random_ip
from .constants import DELIMITER
from .constants import HEADERS
from .constants import HEADERS_INIT_CONVER
from .conversation import Conversation
from .conversation_style import CONVERSATION_STYLE_TYPE
from .request import ChatHubRequest
from .utilities import append_identifier
from .utilities import get_ran_hex
from .utilities import guess_locale
ssl_context = ssl.create_default_context()
ssl_context.load_verify_locations(certifi.where())


class ChatHub:
    def __init__(
        self,
        conversation: Conversation,
        proxy: str = None,
        cookies: Union[List[dict], None] = None,
    ) -> None:
        self.aio_session = None
        self.request: ChatHubRequest
        self.loop: bool
        self.task: asyncio.Task
        self.sec_access_token: str | None = conversation.sec_access_token
        self.request = ChatHubRequest(
            conversation_signature=conversation.struct.get("conversationSignature"),
            client_id=conversation.struct["clientId"],
            conversation_id=conversation.struct["conversationId"],
            blobId=conversation.img_id["blobId"],
        )
        self.cookies = cookies
        self.proxy: str = proxy
        proxy = (
            proxy
            or os.environ.get("all_proxy")
            or os.environ.get("ALL_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTPS_PROXY")
            or None
        )
        if proxy is not None and proxy.startswith("socks5h://"):
            proxy = "socks5://" + proxy[len("socks5h://"):]
        self.session = httpx.AsyncClient(
            proxies=proxy,
            timeout=900,
            headers=HEADERS_INIT_CONVER,
        )

    async def get_conversation(
        self,
        conversation_id: str = None,
        conversation_signature: str = None,
        client_id: str = None,
    ) -> dict:
        conversation_id = conversation_id or self.request.conversation_id
        conversation_signature = (
            conversation_signature or self.request.conversation_signature
        )
        client_id = client_id or self.request.client_id
        url = f"https://sydney.bing.com/sydney/GetConversation?conversationId={conversation_id}&source=cib&participantId={client_id}&conversationSignature={conversation_signature}&traceId={get_ran_hex()}"
        response = await self.session.get(url)
        return response.json()

    async def get_activity(self) -> dict:
        url = "https://www.bing.com/turing/conversation/chats"
        headers = HEADERS_INIT_CONVER.copy()
        if self.cookies is not None:
            for cookie in self.cookies:
                if cookie["name"] == "_U":
                    headers["Cookie"] = f"SUID=A; _U={cookie['value']};"
                    break
        response = await self.session.get(url, headers=headers)
        return response.json()

    async def ask_stream(
        self,
        prompt: str,
        wss_link: str = None,
        conversation_style: CONVERSATION_STYLE_TYPE = None,
        raw: bool = False,
        webpage_context: Union[str, None] = None,
        search_result: bool = False,
        locale: str = guess_locale(),
    ) -> Generator[bool, Union[dict, str], None]:
        """ """
        if self.sec_access_token:
            wss_link = (
                "wss://sydney.bing.com/sydney/ChatHub?sec_access_token="
                + urllib.parse.quote_plus(self.sec_access_token)
            )
        cookies = {}
        if self.cookies is not None:
            for cookie in self.cookies:
                cookies[cookie["name"]] = cookie["value"]
        self.aio_session = aiohttp.ClientSession(cookies=cookies)
        # Check if websocket is closed
        wss = await self.aio_session.ws_connect(
            wss_link or "wss://sydney.bing.com/sydney/ChatHub",
            ssl=ssl_context,
            headers={
                **HEADERS,
                "x-forwarded-for": os.environ.get('xff'),
                    },
            proxy=self.proxy,
        )
        await self._initial_handshake(wss)
        # Construct a ChatHub request
        self.request.update(
            prompt=prompt,
            conversation_style=conversation_style,
            webpage_context=webpage_context,
            search_result=search_result,
            locale=locale,
        )
        # Send request
        await wss.send_str(append_identifier(self.request.struct))
        draw = False
        resp_txt = ""
        result_text = ""
        resp_txt_no_link = ""
        retry_count = 5
        while not wss.closed:
            msg = await wss.receive(timeout=900)
            if not msg.data:
                retry_count -= 1
                if retry_count == 0:
                    raise Exception("No response from server")
                continue
            if isinstance(msg.data, str):
                objects = msg.data.split(DELIMITER)
            else:
                continue
            for obj in objects:
                if int(time()) % 15 == 0:
                    await wss.send_str(append_identifier({"type": 6}))
                if obj is None or not obj:
                    continue
                response = json.loads(obj)
                # print(response)
                if response.get("type") == 1 and response["arguments"][0].get(
                    "messages",
                ):
                    if not draw:
                        if (
                            response["arguments"][0]["messages"][0].get(
                                "messageType",
                            )
                            == "GenerateContentQuery"
                        ):
                            async with ImageGenAsync(
                                all_cookies=json.loads(os.environ.get('image_gen_cookie'))
                            ) as image_generator:
                                images = await image_generator.get_images(
                                    response["arguments"][0]["messages"][0]["text"],
                                )
                            for i, image in enumerate(images):
                                resp_txt = f"{resp_txt}\n![image{i}]({image})"
                            draw = True
                        if (
                            response["arguments"][0]["messages"][0]["contentOrigin"]
                            != "Apology"
                        ) and not draw and not raw:
                            resp_txt = result_text + response["arguments"][0][
                                "messages"
                            ][0]["adaptiveCards"][0]["body"][0].get("text", "")
                            resp_txt_no_link = result_text + response["arguments"][
                                0
                            ]["messages"][0].get("text", "")
                            if response["arguments"][0]["messages"][0].get(
                                "messageType",
                            ):
                                resp_txt = (
                                    resp_txt
                                    + response["arguments"][0]["messages"][0][
                                        "adaptiveCards"
                                    ][0]["body"][0]["inlines"][0].get("text")
                                    + "\n"
                                )
                                result_text = (
                                    result_text
                                    + response["arguments"][0]["messages"][0][
                                        "adaptiveCards"
                                    ][0]["body"][0]["inlines"][0].get("text")
                                    + "\n"
                                )
                        if not raw:
                            yield False, resp_txt

                elif response.get("type") == 2:
                    if response["item"]["result"].get("error"):
                        await self.close()
                        raise Exception(
                            f"{response['item']['result']['value']}: {response['item']['result']['message']}",
                        )
                    if draw:
                        id = 1
                        for i in range(1, len(response["item"]["messages"])):
                            if "adaptiveCards" in response["item"]["messages"][i]:
                                if "text" in response["item"]["messages"][i]["adaptiveCards"][0]["body"][0]:
                                    id = i
                                    break
                        cache=response["item"]["messages"][id]["adaptiveCards"][0]["body"][0]["text"]
                        response["item"]["messages"][id]["adaptiveCards"][0]["body"][0]["text"] = (cache + resp_txt)
                    if (
                        response["item"]["messages"][-1]["contentOrigin"]
                        == "Apology"
                        and resp_txt
                    ):
                        response["item"]["messages"][-1]["text"] = resp_txt_no_link
                        response["item"]["messages"][-1]["adaptiveCards"][0][
                            "body"
                        ][0]["text"] = resp_txt
                        print(
                            "Preserved the message from being deleted",
                            file=sys.stderr,
                        )
                    await wss.close()
                    yield True, response
                    return
                if response.get("type") != 2:
                    if response.get("type") == 6:
                        await wss.send_str(append_identifier({"type": 6}))
                    elif response.get("type") == 7:
                        await wss.send_str(append_identifier({"type": 7}))
                    elif raw:
                        yield False, response

    async def _initial_handshake(self, wss: WebSocketClientProtocol) -> None:
        await wss.send_str(append_identifier({"protocol": "json", "version": 1}))
        await wss.receive_str()
        await wss.send_str(append_identifier({"type": 6}))

    async def delete_conversation(
        self,
        conversation_id: str = None,
        conversation_signature: str = None,
        client_id: str = None,
    ) -> None:
        conversation_id = conversation_id or self.request.conversation_id
        conversation_signature = (
            conversation_signature or self.request.conversation_signature
        )
        client_id = client_id or self.request.client_id
        url = "https://sydney.bing.com/sydney/DeleteSingleConversation"
        await self.session.post(
            url,
            json={
                "conversationId": conversation_id,
                "conversationSignature": conversation_signature,
                "participant": {"id": client_id},
                "source": "cib",
                "optionsSets": ["autosave"],
            },
        )

    async def close(self) -> None:
        await self.session.aclose()
        await self.aio_session.close()
