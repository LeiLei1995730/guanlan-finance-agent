import argparse
import json
import time
from .db import Store
from .collect import Collector
from .research import query,reply_text


def main():
    parser=argparse.ArgumentParser(description='观澜：银行与金融科技研究助手')
    sub=parser.add_subparsers(dest='command',required=True)
    serve=sub.add_parser('serve'); serve.add_argument('--port',type=int,default=18765)
    collect=sub.add_parser('collect'); collect.add_argument('--source')
    ask=sub.add_parser('ask'); ask.add_argument('question')
    digest=sub.add_parser('digest'); digest.add_argument('--days',type=int,default=7); digest.add_argument('--send-to',help='明确发送到允许的飞书群；省略时只预览')
    sub.add_parser('bot')
    watch=sub.add_parser('watch'); watch.add_argument('--minutes',type=int,default=360)
    args=parser.parse_args()
    if args.command=='serve':
        import uvicorn
        from .app import create_app
        uvicorn.run(create_app(),host='127.0.0.1',port=args.port,log_level='warning')
        return
    store=Store()
    if args.command=='collect': print(json.dumps(Collector(store).collect(args.source),ensure_ascii=False,indent=2))
    elif args.command=='ask': print(reply_text(query(store,args.question,channel='cli')))
    elif args.command=='digest':
        if args.send_to:
            from .feishu import send_digest
            print(send_digest(store,args.send_to,args.days))
        else: print(reply_text(query(store,f'最近{args.days}天动态',record=False)))
    elif args.command=='bot':
        from .feishu import run_bot
        run_bot(store)
    elif args.command=='watch':
        collector=Collector(store)
        while True:
            print(json.dumps(collector.collect(),ensure_ascii=False),flush=True)
            time.sleep(max(5,args.minutes)*60)


if __name__=='__main__': main()
