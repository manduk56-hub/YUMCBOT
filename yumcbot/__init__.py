"""YUMCBOT 패키지.

엔트리포인트는 저장소 루트의 bot.py다 (systemd ExecStart, CI, run.ps1이 그 경로를
직접 참조한다). 이 패키지를 import 하는 것만으로 디스코드에 붙지 않도록,
부팅 로직은 yumcbot.main에만 둔다.
"""
