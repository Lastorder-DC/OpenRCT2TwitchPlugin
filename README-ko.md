# OpenRCT2 Twitch Server
OpenRCT2 Twitch Server

# Requirements
- Python 3.6 이상
- flask
- gevent
- requests
- websocket-client<=0.48
- redis
- redislite

# 설치방법
$ pip3 install -r requirements.txt

# 실행방법
## 개발서버
$ python3 openrct2_twitch_server.py -H 0.0.0.0 -p 8000
