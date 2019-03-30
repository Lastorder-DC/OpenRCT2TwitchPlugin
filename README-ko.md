# OpenRCT2 Twitch Server
OpenRCT2 Twitch Server

# Requirements
- Python 3.6 이상
- flask
- gevent
- requests
- websocket-client
- redis

# 설치방법
$ pip3 install -r requirements.txt

# 실행방법
## 개발서버
$ python3 openrct2_twitch_server.py -H 0.0.0.0 -p 8000

## WSGI 서버(gunicorn)
$ gunicorn -b 0.0.0.0:8000 openrct2_twitch_server:app

## 아파치로 요청 프록시하기
아파치의 mod_wsgi 모듈은 gevent spawn 호환성 문제로 지원하지 않습니다.

대신 mod_proxy를 이용해 요청을 gunicorn 같은 WSGI 서버로 프록시하십시오. 실제 서비스에 개발 서버를 사용하는 것은 비권장합니다.
