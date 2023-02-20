#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import logging
import time
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

# 当前访问的是飞书，使用默认存储、默认日志（Error级别），更多可选配置，请看：README.zh.md->如何构建整体配置（Config）
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
    db[uuid].update(conf)
    write_json(DB_FILE, db)


def reset_chat(uuid):
    chatbot.reset_chat()
    set_conf(uuid, dict(conversation_id=None, parent_ids=[]))


def worker():
    while True:
        message_id, open_id, uuid, text = queue.get()
        try:
            handle(message_id, open_id, uuid, text)
        except ChatbotError as e:
            reply_message(reply_message, f"{e.source}({e.code}): {e.message}")
        except Exception:
            traceback.print_exc()
            reply_message(message_id, "服务器异常，请重试")


def handle(message_id, open_id, uuid, text):
    conf = get_conf(uuid)
    conversation_id = conf.get("conversation_id")
    parent_ids = conf.get("parent_ids", [])
    parent_id = parent_ids[-1] if parent_ids else None

    msg = ""
    resp_message_id = None
    last_time = time.time()
    for data in chatbot.ask(text, conversation_id=conversation_id, parent_id=parent_id):
        msg = data["message"]
        if resp_message_id is None:
            # automatically rename for new chat
            if conversation_id is None:
                name = get_user_name(open_id)
                title = conf.get("title", uuid)
                title = f"{name} - {title}"
                chatbot.change_title(data["conversation_id"], title)
                reply_message(message_id, f"开始新对话：{title}")

            resp_message_id = reply_message(message_id, msg, card=True)
        else:
            if time.time() - last_time > 0.3:
                update_message(resp_message_id, msg)
                last_time = time.time()

    if resp_message_id is None:
        log.info(f"no response for conversation {conversation_id}")
        if conversation_id is None:
            reply_message(message_id, f"获取对话结果失败：对话不存在")
        else:
            reply_message(message_id, f"获取对话结果失败：\n{chatbot.get_msg_history(conversation_id)}")
        return

    update_message(resp_message_id, msg)

    parent_ids.append(data["parent_id"])
    conf = dict(conversation_id=data["conversation_id"], parent_ids=parent_ids)
    set_conf(uuid, conf)


def get_user_name(open_id):
    req_call = contact_service.users.get()
    req_call.set_user_id(open_id)
    resp = req_call.do()
    log.debug(f"request id = {resp.get_request_id()}")
    log.debug(f"http status code = {resp.get_http_status_code()}")
    if resp.code != 0:
        return "Unknown"
    return resp.data.user.en_name


def update_message(message_id, msg):
    body = model.MessagePatchReqBody()
    body.content = json.dumps(
        {
            "config": {"wide_screen_mode": True},
            "elements": [
                {
                    "tag": "markdown",
                    "content": msg,
                }
            ],
        }
    )

    req_call = im_service.messages.patch(body)
    req_call.set_message_id(message_id)

    resp = req_call.do()
    log.debug(f"request id = {resp.get_request_id()}")
    log.debug(f"http status code = {resp.get_http_status_code()}")
    if resp.code == 0:
        log.info(f"update {message_id} success")
    else:
        log.error(f"{resp.msg}: {resp.error}")


def reply_message(message_id, msg, card=False):
    body = model.MessageCreateReqBody()
    if card:
        body.content = json.dumps(
            {
                "config": {"wide_screen_mode": True},
                "elements": [
                    {
                        "tag": "markdown",
                        "content": msg,
                    }
                ],
            }
        )
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
        log.info(f"message id = {resp.data.message_id}")
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
            msg += "/title <title>: 修改对话标题（不支持中文）\n"
            msg += "/rollback <n>: 回滚 n 条消息\n"
        elif cmd == "/reset":
            reset_chat(uuid)
            msg = "对话已重新开始"
        else:
            conf = get_conf(uuid)
            conversation_id = conf.get("conversation_id")
            if conversation_id is None:
                msg = "对话不存在"
            elif cmd == "/delete":
                chatbot.delete_conversation(conversation_id)
                reset_chat(uuid)
                msg = "成功删除对话"
            elif cmd == "/title":
                if args:
                    title = args[0].strip()
                    try:
                        set_conf(uuid, dict(title=title))
                        name = get_user_name(open_id)
                        title = f"{name} - {title}"
                        chatbot.change_title(conversation_id, title)
                        msg = f"成功修改标题为：{title}"
                    except Exception:
                        traceback.print_exc()
                        msg = "修改标题失败"
                else:
                    msg = "标题不存在"
            elif cmd == "/rollback":
                if args:
                    n = int(args[0])
                else:
                    n = 1

                conf = get_conf(uuid)
                parent_ids = conf["parent_ids"]
                if 1 <= n <= len(parent_ids):
                    conf["parent_ids"] = parent_ids[:-n]
                    set_conf(uuid, conf)
                    msg = f"成功回滚 {n} 条消息"
                else:
                    msg = "回滚范围不合法"
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
