# wledadmin

Painel web self-hosted para administrar vários dispositivos [WLED](https://kno.wled.ge)
na rede local, inspirado no TasmoAdmin. Pensado para rodar no homelab, ao lado do
Home Assistant.

**Fase 1:**
- Descoberta automática via mDNS (`_wled._tcp`), varredura de faixa de IP e cadastro manual.
- Tabela com nome, IP, MAC, firmware, arquitetura, RSSI, uptime, LEDs, heap livre,
  estado (ligado/brilho/preset) e indicador online/offline.
- Controle em massa: ligar, desligar, alternar, brilho e preset.
- Atualização periódica em segundo plano.
- Login obrigatório (HTTP Basic).

**Fase 2:**
- Backup de `cfg.json` e `presets.json`, manual (em lote) e automático, com histórico.
  Só guarda versões quando o conteúdo muda.
- Diff legível entre versões e entre o backup e o que está no dispositivo.
- Restauração segura, com backup automático antes, verificação depois e suporte a PIN.
- Cópia de presets para vários dispositivos.
- Importação de backups baixados pela própria interface do WLED e exportação em `.zip`.
- Tela de configurações com intervalo do backup automático e retenção.

**Próxima fase (arquitetura já preparada):** OTA em lote.

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

## Backup e restauração

### Como funciona o backup

- **Manual:** selecione dispositivos na tabela e clique em **Fazer backup**, ou use o botão
  na página do dispositivo (clique no nome dele).
- **Automático:** a cada N horas (padrão 24), configurável em **Configurações**. O agendador
  confere a cada 10 min quem está online e com o último backup vencido. Por isso ele não
  perde backups se o wledadmin reiniciar, e faz o backup de quem estava desligado assim
  que voltar.
- **Só o que mudou vira versão.** Se o conteúdo for idêntico ao da última versão (mesmo
  SHA-256), nada novo é gravado, mas a coluna **Backup** da tabela registra a verificação.
  Ela fica destacada quando passa do dobro do intervalo sem verificação.
- **Retenção:** guarda as últimas N versões por dispositivo e tipo (padrão 30). Versões
  **fixadas** nunca são apagadas.
- Os backups ficam no próprio SQLite (`WLEDADMIN_DATA_DIR`). Fazer backup do wledadmin é
  copiar essa pasta (ou o volume `wledadmin-data`).

### O que o backup contém (e o que não contém)

O WLED guarda as senhas (Wi-Fi, AP, MQTT, OTA, PIN de configurações, chave do Hue) num
arquivo separado, o `wsec.json`, que **nunca é servido pela rede**. O `cfg.json` guarda só
o tamanho de cada senha. Portanto:

- os backups do wledadmin não contêm senhas;
- restaurar um `cfg.json` mantém as senhas que o dispositivo já tem;
- num dispositivo resetado de fábrica, o Wi-Fi precisa ser configurado à mão antes.

### Restauração

Na página do dispositivo, clique em **restaurar** numa versão. A tela de confirmação mostra
o diff entre o arquivo atual do dispositivo e o backup, além dos avisos aplicáveis. Ao
confirmar:

1. o conteúdo é validado (JSON do tipo certo) e é verificado se cabe no espaço livre;
2. o arquivo atual vira uma versão "antes de restaurar", então dá para desfazer;
3. se você informar o PIN de configurações, ele é usado só nesta operação e não é salvo;
4. o arquivo é enviado minificado para `POST /upload`;
5. com PIN, as configurações são bloqueadas de novo logo em seguida;
6. **presets:** o arquivo é lido de volta e comparado; **configuração:** o dispositivo
   reinicia sozinho, e o wledadmin espera até 90 s ele voltar com o mesmo MAC.

Regras de segurança:

- **Uma configuração só volta para o mesmo dispositivo (mesmo MAC).** O `cfg.json` contém
  pinos GPIO, IP estático e hostname. Em outro hardware, isso pode gerar conflito de IP ou
  impedir o boot. Não existe opção de "forçar".
- **Backups importados** de configuração exigem uma confirmação extra, porque o wledadmin
  não tem como saber de qual dispositivo o arquivo veio.
- Restaurações e cópias rodam **uma de cada vez**, um dispositivo por vez.
- O WLED grava o upload direto por cima do arquivo. Se a configuração for interrompida no
  meio, o dispositivo pode subir em modo de recuperação (rede Wi-Fi `WLED-AP`). Nesse caso,
  conecte-se a ela, configure o Wi-Fi e restaure a versão "antes de restaurar".

### Copiar presets

Numa versão de presets, clique em **copiar para…** e escolha os dispositivos. O
`presets.json` não tem nada específico de hardware. Cada destino ganha uma versão "antes de
restaurar" no próprio histórico, e a cópia é feita um dispositivo por vez.

### Importar e exportar

- **Importar:** na página do dispositivo, envie um `.json` baixado em
  *Config → Security & Updates → Backup* da interface do WLED. O tipo é detectado sozinho.
- **Exportar:** o link **Exportar backups (.zip)** na tela principal baixa a versão mais
  recente de cada arquivo de cada dispositivo.

## Configuração

O intervalo do backup automático e a retenção ficam na tela **Configurações** (guardados no
banco). O resto são variáveis de ambiente com prefixo `WLEDADMIN_` (ou estão no `.env`).
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

Backups (operações de escrita exigem `"confirm": true`):

```bash
curl -u admin:SENHA -X POST http://localhost:8000/api/backups \
     -H 'content-type: application/json' -d '{"device_ids": [1, 2]}'     # → job
curl -u admin:SENHA http://localhost:8000/api/jobs/1
curl -u admin:SENHA http://localhost:8000/api/devices/1/backups
curl -u admin:SENHA http://localhost:8000/api/backups/7/content -o cfg.json
curl -u admin:SENHA -X POST http://localhost:8000/api/backups/7/restore \
     -H 'content-type: application/json' -d '{"confirm": true, "pin": "1234"}'
curl -u admin:SENHA -X POST http://localhost:8000/api/backups/8/copy \
     -H 'content-type: application/json' -d '{"device_ids": [2, 3], "confirm": true}'
```

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
│   ├── backup.py        # backup, deduplicação, retenção, diff, restore em etapas
│   ├── backup_scheduler.py  # backup automático
│   ├── jobs.py          # jobs em segundo plano com progresso por dispositivo
│   ├── app_settings.py  # configurações editáveis pela tela
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

### Preparação para o OTA (fase 3)

- **Jobs:** o `JobManager` já roda operações em segundo plano com etapas por dispositivo,
  e os jobs "exclusivos" rodam um por vez. É o mesmo modelo que o OTA sequencial vai usar.
- **Backup antes do OTA:** o gatilho `pre_ota` já está previsto. O OTA vai salvar
  `cfg.json` e `presets.json` antes de gravar o firmware.
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
