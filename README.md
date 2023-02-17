# 飞书 ChatGPT 机器人

# 事件订阅地址

`http://ip:8000/webhook/chatgpt`

# 权限

```
contact:contact:readonly_as_app

im:chat
im:chat.group_info:readonly
im:chat:readonly

im:message
im:message.group_at_msg
im:message.p2p_msg
im:message.p2p_msg:readonly
```

# 运行

```sh
cp .env.example .env
cp chatbot.json.example chatbot.json
echo {} > db.json

docker-compose up -d --build
```