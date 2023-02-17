#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import logging
import traceback
from queue import Queue

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
from revChatGPT.V1 import Chatbot
from revChatGPT.V1 import Error as ChatbotError

from file import read_json
from file import write_json

DB_FILE = "db.json"

# 企业自建应用的配置
# AppID、AppSecret: "开发者后台" -> "凭证与基础信息" -> 应用凭证（AppID、AppSecret）
# VerificationToken、EncryptKey："开发者后台" -> "事件订阅" -> 事件订阅（VerificationToken、EncryptKey）
# 更多可选配置，请看：README.zh.md->如何构建应用配置（AppSettings）。
app_settings = Config.new_internal_app_settings_from_env()

# 当前访问的是飞书，使用默认存储、默认日志（Error级别），更多可选配置，请看：README.zh.md->如何构建整体配置（Config）。
conf = Config(DOMAIN_FEISHU, app_settings, log_level=LEVEL_DEBUG)
logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.DEBUG)

im_service = ImService(conf)
contact_service = ContactService(conf)

log = logging.getLogger("bot")

chatbot = Chatbot(read_json("chatbot.json"))

queue = Queue()


def get_conf(uuid):
    db = read_json(DB_FILE, {})
    return db.get(uuid, {})


def set_conf(uuid, conf):
    db = read_json(DB_FILE, {})
    db[uuid] = conf
    write_json(DB_FILE, db)


def worker():
    while True:
        message_id, open_id, uuid, text = queue.get()
        msg = ask(uuid, open_id, text)
        reply_message(message_id, msg)


def ask(uuid, open_id, text):
    conf = get_conf(uuid)
    conversation_id = conf.get("conversation_id")
    parent_id = conf.get("parent_id")
    new_chat = conversation_id is None

    # prevent bug in revChatGPT
    chatbot.conversation_id = None
    chatbot.parent_id = None

    msg = ""
    try:
        for data in chatbot.ask(text, conversation_id=conversation_id, parent_id=parent_id):
            msg = data["message"]

        conversation_id = data["conversation_id"]
        parent_id = data["parent_id"]
        conf = dict(conversation_id=conversation_id)
        set_conf(uuid, conf)
    except ChatbotError as e:
        return f"{e.source}({e.code}): {e.message}"
    except UnboundLocalError:
        return "获取对话结果失败，请重试"
    except Exception:
        traceback.print_exc()
        return "服务器异常，请重试"

    if new_chat:
        name = get_user_name(open_id)
        title = f"{name} - {uuid}"
        chatbot.change_title(conversation_id, title)

    return msg


def get_user_name(open_id):
    req_call = contact_service.users.get()
    req_call.set_user_id(open_id)
    resp = req_call.do()
    log.debug(f"request id = {resp.get_request_id()}")
    log.debug(f"http status code = {resp.get_http_status_code()}")
    log.debug(f"header = {resp.get_header().items()}")
    if resp.code != 0:
        return "Unknown"
    return resp.data.user.en_name


def reply_message(message_id, msg):
    body = model.MessageCreateReqBody()
    body.content = json.dumps(dict(text=msg))
    body.msg_type = "text"

    req_call = im_service.messages.reply(body)
    req_call.set_message_id(message_id)

    resp = req_call.do()
    log.debug(f"request id = {resp.get_request_id()}")
    log.debug(f"http status code = {resp.get_http_status_code()}")
    log.debug(f"header = {resp.get_header().items()}")
    if resp.code == 0:
        log.info(f"message id = {resp.data.message_id}")
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

    text: str = json.loads(message.content).get("text")
    text = text.replace("@_user_1", "").strip()

    open_id = event.event.sender.sender_id.open_id
    uuid = f"{open_id}@{message.chat_id}"
    if text.startswith("/"):
        cmds = text.split()
        cmd = cmds[0]
        args = cmds[1:]
        if cmd == "/help":
            msg = "/help: 查看命令说明\n"
            msg += "/reset: 重新开始对话\n"
            msg += "/delete: 删除当前对话\n"
            msg += "/title: 修改对话标题\n"
        elif cmd == "/reset":
            set_conf(uuid, {})
            msg = "对话已重新开始"
        else:
            conf = get_conf(uuid)
            conversation_id = conf.get("conversation_id")
            if conversation_id is None:
                msg = "对话不存在"
            elif cmd == "/delete":
                chatbot.delete_conversation(conversation_id)
                set_conf(uuid, {})
                msg = "成功删除对话"
            elif cmd == "/title":
                if args:
                    title = args[0].strip()
                    name = get_user_name(open_id)
                    title = f"{name} - {title}"
                    chatbot.change_title(conversation_id, title)
                    msg = f"成功修改标题为：{title}"
                else:
                    msg = "标题不存在"
            else:
                msg = "无效命令"

        reply_message(message.message_id, msg)
    else:
        queue.put_nowait((message.message_id, open_id, uuid, text))


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


# 设置 "开发者后台" -> "事件订阅" 请求网址 URL：https://domain/webhook/event
if __name__ == "__main__":
    from threading import Thread

    thread = Thread(target=worker, args=())
    thread.start()

    app.run(debug=False, port=8000, host="0.0.0.0")
