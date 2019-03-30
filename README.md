### [한국어 문서](README-ko.md)

# OpenRCT2 Twitch Server
OpenRCT2 Twitch Server

# Requirements
- Python 3.6 or above
- flask
- gevent
- requests
- websocket-client
- redis

# How to install
$ pip3 install -r requirements.txt

# How to run
## Development server
$ python3 openrct2_twitch_server.py -H 0.0.0.0 -p 8000

## WSGI server(gunicorn)
$ gunicorn -b 0.0.0.0:8000 openrct2_twitch_server:app

## Using Apache to proxy request
Apache's mod_wsgi is not supported due to gevent spawn compatibility issue.

Use mod_proxy to proxy requests to WSGI server such as gunicorn. Using development server is strongly discouraged.
