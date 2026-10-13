# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Projeto

Painel web self-hosted (FastAPI + Jinja2/HTMX, sem build step) para administrar vários
dispositivos com firmware WLED numa LAN: descoberta, status, ações em massa, backup/restore
de `cfg.json`/`presets.json` e atualização OTA de firmware. UI, mensagens, comentários,
README e commits são em **português do Brasil**.

## Comandos

```bash
uv sync                                   # instala deps (inclui grupo dev)
uv run wledadmin serve                    # sobe em :8000 (login inicial admin/admin)
uv run wledadmin reset-admin              # volta o login para admin/admin
uv run pytest                             # todos os testes
uv run pytest tests/test_ota.py::test_two_step_for_old_esp32   # um teste
uv run ruff check src tests               # lint (line-length 100)
uv run alembic revision --autogenerate -m "descricao"          # nova migração
docker compose up -d --build              # usa network_mode: host (necessário p/ mDNS)
```

As migrações rodam sozinhas no startup (`db.run_migrations`); o `alembic.ini` só serve
para gerar novas. Migrações usam `render_as_batch=True` (SQLite) e ficam em
`src/wledadmin/migrations/versions/` com IDs sequenciais (`0001`, `0002`, ...).

## Arquitetura

**Camadas.** `wled/` é a única camada que fala HTTP com os dispositivos (`WLEDClient`,
schemas tolerantes, análise de binários em `wled/firmware.py`). `services/` tem a lógica;
`web/` só traduz HTTP ↔ serviços. Todos os serviços são montados e ligados em
`services/container.py` (`Services.create` / `start_background` / `close`), e as rotas os
obtêm via `request.app.state.services`.

**Estado em dois lugares, de propósito.** O SQLite guarda só o que muda devagar
(cadastro, versões de backup, histórico de OTA, `app_settings`). Telemetria volátil
(RSSI, uptime, on/bri, online) vive em memória no `StatusStore`. A UI lê só do
`StatusStore`/banco, nunca espera um dispositivo. Por isso o app precisa rodar com **um
único processo/worker**.

**Identidade pelo MAC**, não pelo IP (DHCP). `DeviceRegistry.upsert_from_info` atualiza o
IP quando o mesmo MAC reaparece; o poller trata "outro MAC no IP antigo" como falha.
Backups e histórico de OTA guardam `device_mac` além de `device_id` (que vira NULL se o
dispositivo for removido).

**Concorrência de escrita.** Duas travas que se complementam:
- `DeviceRegistry.lock(device_id)`: serializa operações num mesmo dispositivo. Não é
  reentrante; `BackupService.backup_device` adquire a trava sozinho, então chame-o
  **fora** de um bloco que já segura a trava (veja `OtaService._update_device`).
- `JobManager.start(..., exclusive=True)`: jobs que escrevem nos dispositivos (restore,
  cópia de presets, OTA) rodam um por vez e sequencialmente. Itens registram etapas com
  `JobItem.step()`; falhas esperadas usam `StepFailed` (a mensagem vai para a UI).

**Configuração em duas fontes.** `config.Settings` (env `WLEDADMIN_*` / `.env`) para
infraestrutura e segredos (ex.: `WLEDADMIN_GITHUB_TOKEN`). `services/app_settings.py`
(tabela `app_settings`, editável na tela Configurações) para preferências do usuário:
intervalo/retenção de backup, repositório de firmware, betas, parar no primeiro erro.
Não duplicar a mesma opção nas duas.

**Segurança da UI.** HTTP Basic obrigatório (`web/security.py`, senha em Argon2id com
cache HMAC curto). `SameOriginMiddleware` bloqueia POST/DELETE com `Origin`/`Referer` de
outro host (CSRF, já que o navegador reenvia Basic). PIN do WLED nunca é persistido e
nunca deve ir para query string: formulários que podem conter PIN recalculam por POST.

**HTMX.** `hx-target` é herdado do elemento pai: elementos com polling dentro de um
`<form>` que tem `hx-target` precisam de `hx-target="this"`. A tabela principal preserva
a seleção mandando os checkboxes marcados via `hx-include`. Respostas usam o header
`HX-Trigger: devices-changed` para atualizar a tabela.

## Fatos da API do WLED que o código depende

Conferidos no código-fonte do WLED (0.14.4, 0.15.x e `main`/16.x). Não suponha nomes de
campos: confira em `wled00/json.cpp`, `wled_server.cpp`, `cfg.cpp`, `ota_update.cpp`.

- Campos variam por versão: `release`, `repo`, `deviceId`, `flash` não existem em
  versões antigas; `arch` vem como `"esp8266"`/`"esp32"` ou `ESP.getChipModel()`
  (`"ESP32-C3"`). Todo campo em `wled/schemas.py` é opcional e tolerante a tipo errado.
- Polling usa `GET /json/si` (estado + info) com fallback; ações usam `POST /json/state`
  com `"v": true` para receber o estado de volta. O conjunto de ações é fechado
  (`services/actions.py`), sem JSON livre.
- `GET /cfg.json` e `/presets.json` não exigem PIN; `wsec.json` (senhas) nunca é servido,
  então backups não contêm senhas. `POST /upload` e `POST /update` exigem `correctPIN`;
  enviar `{"pin": ...}` num POST JSON desbloqueia globalmente (15 min); `GET
  /settings/lock` bloqueia. Upload de `cfg.json` e OTA reiniciam o dispositivo.
- `cfg.json` só pode ser restaurado no mesmo MAC (tem GPIOs, IP estático, hostname).
- OTA: o 0.14/0.15 grava qualquer binário; só o 16.x valida o release embutido. O
  wledadmin valida por conta própria (`wled/firmware.py`: magic `0xE9`, `chip_id` no
  offset 12 para ESP32, flash no nibble alto do byte 3 para ESP8266, struct de metadados
  com magic `0x57535453` e hash DJB2). **Nunca enviar `skipValidation`.** Downgrade é
  bloqueado; a versão mínima segura gera duas etapas na escolha automática e só aviso
  em `.bin` próprio/escolha manual.
  O backup `pre_ota` é opcional (caixa marcada por padrão). Jobs de OTA têm
  `Job.kind == "ota"`, e itens com falha levam a `/firmware/recover/<job>/<device>`, que
  usa `OtaService.diagnose()` (estado ao vivo × backup mais novo) para sugerir restore.
- Requisições aos ESP usam keep-alive desligado (poucos sockets no ESP; não roubar vaga
  do Home Assistant). Polling foi escolhido em vez do WebSocket `/ws` pelo mesmo motivo.

## Testes

Nenhum teste toca rede ou hardware real. `tests/fakes.py` tem:
- `FakeWLEDNetwork` / `FakeDevice`: dispositivos simulados por IP (offline, timeout, PIN
  global, upload multipart, reboot, OTA lock, falha de OTA, validação de release do 16.x).
- `FakeGitHub` / `standard_github()`: API de releases + downloads com digest SHA-256.
- `make_firmware(...)`: binários sintéticos com o mesmo layout dos reais. Não commitar
  binários do WLED.

Ambos entram como transporte httpx: `create_app(settings, start_background=False,
wled_transport=..., github_transport=...)` (fixtures `app_client` em `conftest.py` e
`fw_client` em `test_web_firmware.py`). Serviços com espera de reboot aceitam atrasos
injetáveis (`reboot_initial_delay`, `reboot_poll_interval`) para os testes não dormirem.
Fixtures JSON de `/json/info` das versões 0.14, 0.15 e 16.x ficam em `tests/fixtures/`.
`pytest-asyncio` está em modo `auto`.

## Como o dono do projeto quer trabalhar

- Para funcionalidades novas, apresentar o plano (estrutura, modelo de dados, fluxos e o
  porquê) e esperar aprovação antes de escrever código.
- Fazer commit e push só quando pedido.
