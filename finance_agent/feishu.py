"""Official SDK persistent connection; no public webhook or group-history access."""
import hashlib
import json
import os
import re
import threading
import time
from .db import Store
from .research import query, reply_text


def accepted_message(event, allowed_chats):
    sender=event.get('sender',{})
    msg=event.get('message',{})
    if sender.get('sender_type')!='user' or msg.get('message_type')!='text': return None
    if msg.get('chat_id') not in allowed_chats: return None
    if msg.get('chat_type')=='group' and not msg.get('mentions'): return None
    try: text=json.loads(msg.get('content','{}')).get('text','')
    except (ValueError,TypeError): return None
    text=re.sub(r'@_user_\d+','',text).strip()
    if not text or not msg.get('message_id'): return None
    return dict(message_id=msg['message_id'],question=text[:500],chat_id=msg['chat_id'],actor=sender.get('sender_id',{}).get('open_id','unknown'))


def run_bot(store=None):
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import ReplyMessageRequest,ReplyMessageRequestBody
    store=store or Store()
    app_id=os.getenv('FEISHU_APP_ID'); secret=os.getenv('FEISHU_APP_SECRET')
    allowed={x.strip() for x in os.getenv('FEISHU_ALLOWED_CHATS','').split(',') if x.strip()}
    if not app_id or not secret or not allowed:
        raise SystemExit('请在运行环境设置 FEISHU_APP_ID、FEISHU_APP_SECRET、FEISHU_ALLOWED_CHATS。')
    client=lark.Client.builder().app_id(app_id).app_secret(secret).log_level(lark.LogLevel.ERROR).build()

    def handle(data):
        # Callback only enqueues; returns promptly while the worker performs retrieval.
        event=json.loads(lark.JSON.marshal(data)).get('event',{})
        payload=accepted_message(event,allowed)
        if payload:
            store.enqueue('feishu:'+payload['message_id'],payload)

    def worker():
        while True:
            job=store.claim_job()
            if not job: time.sleep(1); continue
            p=job['payload']
            try:
                result=query(store,p['question'],p['actor'],'feishu',p['chat_id'],record=False)
                # Stable UUID also asks the Feishu server to suppress retried replies.
                stable=hashlib.sha256(job['id'].encode()).hexdigest()[:32]
                body=ReplyMessageRequestBody.builder().msg_type('text').content(json.dumps({'text':reply_text(result)},ensure_ascii=False)).uuid(stable).build()
                request=ReplyMessageRequest.builder().message_id(p['message_id']).request_body(body).build()
                response=client.im.v1.message.reply(request)
                if not response.success(): raise RuntimeError('Feishu API rejected reply')
                store.usage('query',p['actor'],'feishu',p['chat_id'],result['mode'])
                store.finish_job(job,True)
            except Exception as e:
                store.finish_job(job,False,type(e).__name__)

    threading.Thread(target=worker,daemon=True).start()
    handler=lark.EventDispatcherHandler.builder('','').register_p2_im_message_receive_v1(handle).build()
    print('飞书助手已启动，只响应配置群内的 @ 消息或已允许的私聊。')
    lark.ws.Client(app_id,secret,event_handler=handler,log_level=lark.LogLevel.ERROR).start()


def send_digest(store, chat_id, days=7):
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import CreateMessageRequest,CreateMessageRequestBody
    allowed={x.strip() for x in os.getenv('FEISHU_ALLOWED_CHATS','').split(',') if x.strip()}
    if chat_id not in allowed: raise ValueError('目标群不在已配置的允许清单中')
    client=lark.Client.builder().app_id(os.environ['FEISHU_APP_ID']).app_secret(os.environ['FEISHU_APP_SECRET']).log_level(lark.LogLevel.ERROR).build()
    result=query(store,f'银行与金融科技最近{days}天动态',use_ai=True,record=False)
    from .db import now
    stable=hashlib.sha256((chat_id+now()[:10]+'digest'+str(days)).encode()).hexdigest()[:32]
    if store.rows("SELECT id FROM jobs WHERE id=? AND status='done'",('digest:'+stable,)):
        return {'status':'already_sent'}
    body=CreateMessageRequestBody.builder().receive_id(chat_id).msg_type('text').content(json.dumps({'text':reply_text(result)},ensure_ascii=False)).uuid(stable).build()
    request=CreateMessageRequest.builder().receive_id_type('chat_id').request_body(body).build()
    response=client.im.v1.message.create(request)
    if not response.success(): raise RuntimeError('飞书发送失败，未计入成功推送量')
    with store.conn() as c:
        c.execute("INSERT OR REPLACE INTO jobs(id,payload,status,due_at,created_at) VALUES(?,'{}','done',?,?)",('digest:'+stable,now(),now()))
    store.usage('digest','bot','feishu',chat_id)
    return {'status':'sent'}
