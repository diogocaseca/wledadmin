# wledadmin

Painel web self-hosted para administrar vários dispositivos [WLED](https://kno.wled.ge)
na rede local, inspirado no TasmoAdmin. Pensado para rodar no homelab, ao lado do
Home Assistant.

**Fase 1 (atual):**
- Descoberta automática via mDNS (`_wled._tcp`), varredura de faixa de IP e cadastro manual.
- Tabela com nome, IP, MAC, firmware, arquitetura, RSSI, uptime, LEDs, heap livre,
  estado (ligado/brilho/preset) e indicador online/offline.
- Controle em massa: ligar, desligar, alternar, brilho e preset.
- Atualização periódica em segundo plano.
- Login obrigatório (HTTP Basic).

**Próximas fases (arquitetura já preparada):** backup/restauração de `cfg.json` e
`presets.json` com histórico, e OTA em lote.

---

## Rodando localmente

Requisitos: Python 3.12+ e [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env      # opcional: ajuste WLEDADMIN_SCAN_SUBNETS etc.
uv run wledadmin serve
```

Abra <http://localhost:8000>. O login inicial é **admin / admin**. O painel mostra um
aviso até você trocar a senha em **Conta**.

### Testes

Os testes não precisam de dispositivo real: usam respostas mockadas das versões
0.14, 0.15 e 16.x da API do WLED (veja `tests/fixtures/`).

```bash
uv run pytest
uv run ruff check src tests
```

## Rodando com Docker

```bash
cp .env.example .env      # opcional
docker compose up -d --build
```

O painel fica em `http://<ip-do-host>:8000`. Os dados (SQLite) ficam no volume
`wledadmin-data`.

### Por que `network_mode: host`?

A descoberta mDNS usa multicast (`224.0.0.251:5353`), que **não atravessa a rede bridge
padrão do Docker**. Por isso o `docker-compose.yml` usa `network_mode: host`. Com isso:

- a seção `ports:` é ignorada, e o app escuta direto na porta `WLEDADMIN_PORT` do host;
- para mudar a porta, defina `WLEDADMIN_PORT` no `.env`;
- `network_mode: host` só funciona assim no **Docker Engine em Linux**. No Docker Desktop
  (macOS/Windows) o "host" é uma VM e o mDNS não chega à sua LAN.

Se não puder usar `host`, há duas alternativas:

1. **Rede bridge sem mDNS:** troque `network_mode: host` por `ports: ["8000:8000"]` e
   defina `WLEDADMIN_MDNS_ENABLED=false`. A varredura de IP e o cadastro manual
   funcionam normalmente, porque são só HTTP.
2. **macvlan:** dê ao contêiner um IP próprio na LAN. O mDNS funciona, mas o host
   não consegue falar com o contêiner sem configuração extra.

Rode **um único contêiner/worker**: o status ao vivo e as varreduras ficam em memória.

## Login e senha

- A autenticação HTTP Basic é obrigatória em todas as páginas e na API. Só `/healthz`
  e os arquivos estáticos (`/static`) são públicos.
- A senha é guardada como **hash Argon2id**, nunca em texto. Não é possível recuperar a
  senha original, só redefini-la.
- Para trocar usuário e senha, use a página **Conta**. Depois de salvar, o navegador pede
  as novas credenciais.
- Esqueceu a senha? Volte para `admin/admin` com:

  ```bash
  uv run wledadmin reset-admin
  # ou, no Docker:
  docker compose exec wledadmin wledadmin reset-admin
  ```

- O HTTP Basic envia a senha a cada requisição (em base64, que não é criptografia).
  Fora da sua LAN, ou se a rede não for confiável, coloque o painel atrás de um reverse
  proxy com HTTPS.
- Requisições `POST`/`DELETE` vindas de outro site (cabeçalho `Origin` diferente) são
  bloqueadas, para que uma página maliciosa não use a sessão do seu navegador para
  desligar as luzes. Scripts sem `Origin` (curl) funcionam normalmente.

## Configuração

Todas as opções são variáveis de ambiente com prefixo `WLEDADMIN_` (ou estão no `.env`).
Veja [.env.example](.env.example). As principais:

| Variável | Padrão | Descrição |
|---|---|---|
| `WLEDADMIN_PORT` | `8000` | Porta HTTP |
| `WLEDADMIN_DATA_DIR` | `./data` (`/data` no Docker) | Banco SQLite e dados futuros |
| `WLEDADMIN_POLL_INTERVAL` | `10` | Segundos entre rodadas de polling |
| `WLEDADMIN_POLL_CONCURRENCY` | `16` | Requisições simultâneas |
| `WLEDADMIN_HTTP_TIMEOUT` | `4` | Timeout por dispositivo (s) |
| `WLEDADMIN_OFFLINE_AFTER_FAILURES` | `2` | Falhas seguidas até marcar offline |
| `WLEDADMIN_OFFLINE_POLL_EVERY` | `6` | Offline são consultados a cada N ciclos |
| `WLEDADMIN_MDNS_ENABLED` | `true` | Liga a descoberta mDNS |
| `WLEDADMIN_SCAN_SUBNETS` | vazio | Faixas padrão da varredura, ex.: `192.168.1.0/24` |

## API JSON

Mesma autenticação do painel. A documentação interativa fica em `/docs`.

```bash
curl -u admin:SENHA http://localhost:8000/api/devices
curl -u admin:SENHA -X POST http://localhost:8000/api/devices \
     -H 'content-type: application/json' -d '{"host": "192.168.1.50"}'
curl -u admin:SENHA -X POST http://localhost:8000/api/actions \
     -H 'content-type: application/json' \
     -d '{"device_ids": [1, 2], "action": {"kind": "brightness", "value": 128}}'
curl -u admin:SENHA -X POST http://localhost:8000/api/scan \
     -H 'content-type: application/json' -d '{"subnets": ["192.168.1.0/24"]}'
```

`kind` aceita `on`, `off`, `toggle`, `brightness` (0–255) e `preset` (1–250).

---

## Arquitetura

```
src/wledadmin/
├── wled/            # ÚNICA camada que fala com o WLED (cliente httpx + schemas tolerantes)
├── services/
│   ├── registry.py      # cadastro (SQLite), upsert por MAC, lock por dispositivo
│   ├── status_store.py  # último status de cada dispositivo, em memória
│   ├── poller.py        # polling periódico concorrente
│   ├── actions.py       # ações em massa (conjunto fechado e validado)
│   ├── auth.py          # credencial do admin (Argon2id)
│   ├── discovery/       # mDNS, varredura e probe comum
│   └── container.py     # monta e liga/desliga tudo
├── web/             # rotas FastAPI, templates Jinja2 + HTMX (sem build step)
└── migrations/      # Alembic (aplicadas automaticamente no startup)
```

### Decisões

- **Identidade pelo MAC, não pelo IP.** O DHCP muda IPs. Quando um dispositivo reaparece
  em outro IP (via mDNS ou varredura), o registro é atualizado e não duplicado. Se outro
  WLED passar a responder no IP antigo, o poller detecta o MAC diferente e não mistura
  os dados.
- **Telemetria em memória, cadastro no SQLite.** RSSI, uptime, heap e on/bri mudam a cada
  ciclo, e gravá-los no banco só desgastaria o disco. No banco vão nome, versão, arch e
  `release` (quando mudam) e `last_seen_at` (no máximo a cada 5 min).
- **A UI nunca espera um dispositivo.** A tabela é atualizada a cada 5 s lendo só o cache.
  Com 50 dispositivos offline, o painel continua instantâneo.
- **Polling resiliente.** Usa um único `GET /json/si` (estado + info) por dispositivo,
  com fallback para `/json/state` + `/json/info` se o firmware não tiver `/json/si`. As
  consultas rodam em paralelo com semáforo e timeout curto, e cada dispositivo tem o
  próprio tratamento de erro. O dispositivo só vira offline depois de 2 falhas seguidas,
  e a partir daí entra em backoff.
- **Sem keep-alive HTTP.** O servidor web do ESP tem pouquíssimos sockets (principalmente
  no ESP8266). Uma conexão ociosa nossa poderia tirar a vaga do Home Assistant.
- **Ações com `"v": true`.** O WLED devolve o estado completo na resposta do POST, então a
  tabela reflete a ação sem esperar o próximo ciclo.
- **SQLAlchemy 2.0 async + Alembic** (em vez de SQLModel). O suporte async é maduro, e as
  tabelas de backup e OTA das próximas fases vão exigir migrações sem perder dados.

### Por que polling e não o WebSocket `/ws` do WLED

O `/ws` envia estado + info na conexão e a cada mudança, o que é atraente. Mesmo assim,
a fase 1 usa polling:

- cada WebSocket fica aberto e consome heap do ESP. No ESP8266, quando falta memória, o
  WLED derruba **todos** os clientes WS;
- o número de clientes simultâneos é pequeno, e a integração WLED do Home Assistant já
  mantém um WS por dispositivo. Mais um, permanente, de um painel de administração
  disputaria essas vagas;
- reconexão e backoff de N conexões persistentes é bem mais complexo que um `gather`
  periódico;
- para administração, 5–10 s de atraso é aceitável, e as ações do próprio painel já
  aparecem na hora.

O poller fica isolado em `services/poller.py`. Um modo WS opcional por dispositivo pode
ser adicionado depois sem mexer na UI nem no cache.

### Preparação para as próximas fases

- **Backup:** o `WLEDClient.get_json_file()` já lê `cfg.json` e `presets.json`. Falta só
  uma tabela `backups` e o armazenamento em `WLEDADMIN_DATA_DIR`.
- **OTA:** a coluna `release` (0.15+) identifica a variante do build e, junto com `arch`,
  vai servir para escolher o `.bin` correto. O `DeviceRegistry.lock()` já serializa
  operações por dispositivo, para que backup, OTA e ações em massa não colidam. O
  conjunto fechado de ações e a ausência de "JSON livre" seguem o mesmo princípio que o
  OTA vai exigir: confirmação explícita e execução sequencial.

---

## Licença

[MIT](LICENSE) © 2026 Diogo Caseca.

O wledadmin não contém código do WLED (licenciado sob EUPL-1.2): apenas se comunica com
a API HTTP dos dispositivos. O `htmx.min.js` incluído em `src/wledadmin/web/static/` é
distribuído sob a licença Zero-Clause BSD.
