# cmux-remote-mirror

Remote makinedeki [cmux](https://github.com/manaflow-ai/cmux) workspace ve pane yapısını lokal cmux'te aynı isimlerle oluşturur. Her lokal pane, remote'daki ilgili tmux session'ına SSH ile bağlanır.

## Gereksinimler

- [uv](https://github.com/astral-sh/uv)
- Lokal ve remote makinede [cmux](https://github.com/manaflow-ai/cmux)
- Remote makinede tmux
- SSH erişimi (varsayılan host: `home`)

## Nasıl Çalışır

1. Remote makineye SSH ile bağlanıp cmux workspace/pane/surface yapısını ve tmux session listesini alır
2. Her surface'in tmux status bar'ından session adını okuyarak surface-tmux eşleştirmesi yapar
3. Lokal cmux'te aynı isimlerle workspace ve pane'ler oluşturur
4. Her pane'e `ssh -t <host> tmux attach-session -t <session>` komutu gönderir

## Kullanım

```bash
# Varsayılan host (home)
./cmux-remote-mirror.py

# Farklı host
./cmux-remote-mirror.py myhost
```

> **Not:** Script'i cmux terminal'inin içinden çalıştırın (cmux CLI socket erişimi gereklidir).
