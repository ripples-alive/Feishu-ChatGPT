#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import logging
import time
import traceback
from os import environ
from queue import Queue
from uuid import uuid4

from flask import Flask
from flask import request
from flask.helpers import make_response
from larksuiteoapi import DOMAIN_FEISHU
from larksuiteoapi import LEVEL_DEBUG
from larksuiteoapi import Config
from larksuiteoapi import Context
from larksuiteoapi.event import handle_event
from larksuiteoapi.model import OapiHeader
from larksuiteoapi.model import OapiRequest
from larksuiteoapi.service.contact.v3 import Service as ContactService
from larksuiteoapi.service.im.v1 import MessageReceiveEvent
from larksuiteoapi.service.im.v1 import MessageReceiveEventHandler
from larksuiteoapi.service.im.v1 import Service as ImService
from larksuiteoapi.service.im.v1 import model
from revChatGPT.typings import Error as ChatGPTError
from revChatGPT.V1 import Chatbot

from file import read_json
from file import write_json

DB_FILE = "db.json"
LOADING_IMG_KEY = environ.get("LOADING_IMG_KEY")

ALL_MODELS = {
    "default": "text-davinci-002-render-sha",
    "legacy": "text-davinci-002-render-paid",
    "gpt-4": "gpt-4",
}

# 企业自建应用的配置
# AppID、AppSecret: "开发者后台" -> "凭证与基础信息" -> 应用凭证（AppID、AppSecret）
# VerificationToken、EncryptKey："开发者后台" -> "事件订阅" -> 事件订阅（VerificationToken、EncryptKey）
# 更多可选配置，请看：README.zh.md->如何构建应用配置（AppSettings）。
app_settings = Config.new_internal_app_settings_from_env()

# 当前访问的是飞书，使用默认存储、默认日志（Error级别），更多可选配置，请看：README.zh.md->如何构建整体配置（Config）
conf = Config(DOMAIN_FEISHU, app_settings, log_level=LEVEL_DEBUG)
log_level = logging.getLevelName(environ.get("LOG_LEVEL").upper() or "INFO")
logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s (%(funcName)s): %(message)s", level=log_level, force=True
)

im_service = ImService(conf)
contact_service = ContactService(conf)

log = logging.getLogger("bot")

keys = ["email", "password", "session_token", "access_token", "proxy"]
bot_conf = {k: environ.get(k.upper()) for k in keys}
bot_conf = {k: v for k, v in bot_conf.items() if v}
chatbot = Chatbot(bot_conf)

cmd_queue = Queue()
msg_queue = Queue()


def get_conf(uuid):
    db = read_json(DB_FILE, {})
    return db.get(uuid, {})


def set_conf(uuid, conf):
    db = read_json(DB_FILE, {})
    db.setdefault(uuid, {}).update(conf)
    write_json(DB_FILE, db)


def worker(queue):
    def decorator(func):
        def wrapper():
            while True:
                args = queue.get()
                message_id = args[0]
                try:
                    msg = func(*args)
                    if msg is not None:
                        reply_message(message_id, msg)
                except ChatGPTError as e:
                    reply_message(message_id, f"{e.source}({e.code}): {e.message}")
                except Exception as e:
                    traceback.print_exc()
                    reply_message(message_id, f"服务器异常: {e}")

        return wrapper

    return decorator


@worker(cmd_queue)
def handle_cmd(message_id, open_id, chat_id, text):
    uuid = f"{open_id}@{chat_id}"

    if not text.startswith("/"):
        conf = get_conf(uuid)
        conversation_id = conf.get("conversation_id")
        parent_ids = conf.get("parent_ids", [])
        model = conf.get("model")

        name = get_user_name(open_id)
        title = conf.get("title")
        if title is None:
            title = get_group_name(chat_id)
        title = f"{name} - {title}"

        if conversation_id is None:
            reply_message(message_id, f"开始新对话：{title}")

        resp_message_id = reply_message(message_id, "", card=True)

        msg_queue.put_nowait((message_id, resp_message_id, title, uuid, text, conversation_id, parent_ids, model))
        return

    cmds = text.split()
    cmd = cmds[0]
    args = cmds[1:]
    if cmd == "/help":
        msg = "/help: 查看命令说明\n"
        msg += "/reset: 重新开始对话\n"
        msg += "/title <title>: 修改对话标题，为空则表示清除标题设置\n"
        msg += f"/model <model>: 修改使用的模型（{', '.join(ALL_MODELS)}）\n"
        msg += "/rollback <n>: 回滚 n 条消息\n"
        return msg

    conf = get_conf(uuid)
    conversation_id = conf.get("conversation_id")

    if cmd == "/reset":
        chatbot.reset_chat()
        set_conf(uuid, dict(conversation_id=None, parent_ids=[]))
        if conversation_id is not None:
            chatbot.delete_conversation(conversation_id)
        return "对话已重新开始"
    elif cmd == "/title":
        if args:
            title = args[0].strip()
        else:
            title = None

        set_conf(uuid, dict(title=title))

        if title is None:
            return "成功清除标题设置"

        if conversation_id is not None:
            name = get_user_name(open_id)
            title = f"{name} - {title}"
            chatbot.change_title(conversation_id, title)
        return f"成功修改标题为：{title}"

    if cmd == "/model":
        if not args:
            return "模型不存在"

        model = args[0].strip().lower()
        if model not in ALL_MODELS:
            return "模型不存在"

        set_conf(uuid, dict(model=ALL_MODELS[model]))
        return f"成功修改模型为：{model} ({ALL_MODELS[model]})"

    if conversation_id is None:
        return "对话不存在"

    if cmd == "/rollback":
        if args:
            n = int(args[0])
        else:
            n = 1

        conf = get_conf(uuid)
        parent_ids = conf["parent_ids"]
        if not 1 <= n <= len(parent_ids):
            return "回滚范围不合法"

        conf["parent_ids"] = parent_ids[:-n]
        set_conf(uuid, conf)
        return f"成功回滚 {n} 条消息"

    return "无效命令"


@worker(msg_queue)
def handle_msg(_, resp_message_id, title, uuid, text, conversation_id, parent_ids, model):
    conversation_id = conversation_id or uuid4()
    parent_id = parent_ids[-1] if parent_ids else None

    msg = ""
    last_time = time.time()
    for data in chatbot.ask(text, conversation_id=conversation_id, parent_id=parent_id, model=model):
        msg = data["message"]
        if time.time() - last_time > 0.3:
            update_message(resp_message_id, msg)
            last_time = time.time()

    if not msg:
        log.warn(f"no response for conversation {conversation_id}")
        if conversation_id is None:
            return "获取对话结果失败：对话不存在"
        else:
            return f"获取对话结果失败：\n{chatbot.get_msg_history(conversation_id)}"

    update_message(resp_message_id, msg, finish=True)

    parent_ids.append(data["parent_id"])
    conf = dict(conversation_id=data["conversation_id"], parent_ids=parent_ids)
    set_conf(uuid, conf)

    # automatically rename everytime
    chatbot.change_title(data["conversation_id"], title)


def get_user_name(open_id):
    req_call = contact_service.users.get()
    req_call.set_user_id(open_id)
    resp = req_call.do()
    log.debug(f"request id = {resp.get_request_id()}")
    log.debug(f"http status code = {resp.get_http_status_code()}")
    if resp.code != 0:
        log.error(f"{resp.msg}: {resp.error}")
        return "Unknown"
    log.info(f"user: {resp.data.user.name} ({resp.data.user.en_name})")
    return resp.data.user.name


def get_group_name(chat_id):
    req_call = im_service.chats.get()
    req_call.set_chat_id(chat_id)
    resp = req_call.do()
    log.debug(f"request id = {resp.get_request_id()}")
    log.debug(f"http status code = {resp.get_http_status_code()}")
    if resp.code != 0:
        log.error(f"{resp.msg}: {resp.error}")
        return f"<{chat_id}>"
    if resp.data.chat_mode != "group":
        log.info(f"group mode: {resp.data.chat_mode}")
        return f"[{resp.data.chat_mode}]"
    log.info(f"group: {resp.data.name}")
    return resp.data.name


def convert_to_card(msg, finish=False):
    elements = [{"tag": "div", "text": {"tag": "plain_text", "content": msg}}]
    if not finish:
        notes = []
        if LOADING_IMG_KEY:
            notes.append(
                {
                    "tag": "img",
                    "img_key": LOADING_IMG_KEY,
                    "alt": {"tag": "plain_text", "content": ""},
                },
            )
        notes.append({"tag": "plain_text", "content": "typing..."})
        elements.append({"tag": "note", "elements": notes})
    return {"config": {"wide_screen_mode": True}, "elements": elements}


def update_message(message_id, msg, finish=False):
    body = model.MessagePatchReqBody()
    body.content = json.dumps(convert_to_card(msg, finish))

    req_call = im_service.messages.patch(body)
    req_call.set_message_id(message_id)

    resp = req_call.do()
    log.debug(f"request id = {resp.get_request_id()}")
    log.debug(f"http status code = {resp.get_http_status_code()}")
    if resp.code == 0:
        log.info(f"update {message_id} success")
    else:
        log.error(f"{resp.msg}: {resp.error}")


def reply_message(message_id, msg, card=False, finish=False):
    body = model.MessageCreateReqBody()
    if card:
        body.content = json.dumps(convert_to_card(msg, finish))
        body.msg_type = "interactive"
    else:
        body.content = json.dumps(dict(text=msg))
        body.msg_type = "text"

    req_call = im_service.messages.reply(body)
    req_call.set_message_id(message_id)

    resp = req_call.do()
    log.debug(f"request id = {resp.get_request_id()}")
    log.debug(f"http status code = {resp.get_http_status_code()}")
    if resp.code == 0:
        log.info(f"reply for {message_id}: {resp.data.message_id}")
        return resp.data.message_id
    else:
        log.error(f"{resp.msg}: {resp.error}")


def message_receive_handle(ctx: Context, conf: Config, event: MessageReceiveEvent) -> None:
    log.debug(f"request id = {ctx.get_request_id()}")
    log.debug(f"header = {event.header}")
    log.debug(f"event = {event.event}")

    message = event.event.message
    if message.message_type != "text":
        log.warning("unhandled message type")
        reply_message(message.message_id, "暂时只能处理文本消息")
        return

    open_id = event.event.sender.sender_id.open_id

    text: str = json.loads(message.content).get("text")
    log.info(f"<{open_id}@{message.chat_id}> {message.message_id}: {text}")
    text = text.replace("@_user_1", "").strip()

    cmd_queue.put_nowait((message.message_id, open_id, message.chat_id, text))


MessageReceiveEventHandler.set_callback(conf, message_receive_handle)

app = Flask(__name__)


@app.route("/webhook/chatgpt", methods=["GET", "POST"])
def webhook_event():
    oapi_request = OapiRequest(uri=request.path, body=request.data, header=OapiHeader(request.headers))
    resp = make_response()
    oapi_resp = handle_event(conf, oapi_request)
    resp.headers["Content-Type"] = oapi_resp.content_type
    resp.data = oapi_resp.body
    resp.status_code = oapi_resp.status_code
    return resp


# 设置 "开发者后台" -> "事件订阅" 请求网址 URL：https://domain/webhook/chatgpt
if __name__ == "__main__":
    from threading import Thread

    for i in range(2):
        Thread(target=handle_cmd, args=()).start()

    # Only one message at a time allowed for ChatGPT website
    Thread(target=handle_msg, args=()).start()

    app.run(debug=False, port=8000, host="0.0.0.0")
