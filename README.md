# Memobook Clone

Um tutor de conversação para macOS. Escolha uma situação, prepare um livro de prática, converse com o tutor e use a revisão na próxima sessão. O microfone aceita português e inglês. Esta é uma implementação independente inspirada no [MemoBook](https://memobook.ai/).

## Rodar no Mac

Com Homebrew, instale as dependências e execute o aplicativo a partir da raiz do repositório.

```sh
brew install python ffmpeg whisper-cpp llama.cpp
./run.sh
```

Abra [o Studio local](http://127.0.0.1:4827/app). Na primeira execução, o script baixa o Qwen2.5-7B e o Whisper large-v3-turbo e verifica os arquivos por SHA-256. Reserve aproximadamente 6 GB em disco para os modelos. Por padrão, as respostas são geradas pelo Qwen, as gravações são transcritas no Mac e a voz usa o sintetizador do macOS. Os livros e as sessões ficam no navegador.

## Azure e ElevenLabs

A integração opcional com Azure usa recursos da sua conta. Crie a pasta local com `mkdir -p .state && chmod 700 .state`. Guarde as chaves nos arquivos `.state/azure-chat.key` e `.state/azure-voice.key`, restritos ao proprietário. Salve os endpoints e nomes dos deployments em `.state/azure.json`.

```json
{
  "chat_endpoint": "https://YOUR-CHAT-RESOURCE.openai.azure.com",
  "chat_model": "YOUR-CHAT-DEPLOYMENT",
  "voice_endpoint": "https://YOUR-VOICE-RESOURCE.openai.azure.com",
  "voice_model": "YOUR-SPEECH-DEPLOYMENT",
  "daily_request_limit": 600
}
```

Substitua os campos por seus valores. O deployment de chat precisa aceitar respostas estruturadas e esforço de raciocínio baixo. Para usar ElevenLabs, salve sua chave em `.state/elevenlabs.key` com permissão `0600`. A narração usa `eleven_v4` e o modo conversa usa `eleven_v4_turbo`. O texto para síntese é enviado ao provedor de voz escolhido. O áudio do microfone é transcrito localmente.

Para provisionar Azure com Terraform, escolha sua assinatura com a Azure CLI, informe um grupo de recursos já existente em `TF_VAR_resource_group` e um prefixo globalmente único em `TF_VAR_account_prefix`. Execute `infra/connect.sh plan` para revisar. `infra/connect.sh apply` cria recursos faturáveis e grava a configuração em `.state/`. Os modelos, versões, regiões e capacidades estão em `infra/main.tf`.

## Testar

As rotas do servidor têm um teste próprio que não precisa do Studio, dos modelos nem do navegador: `python3 server.test.py` (ou `npm run test:server`).

Com o Studio rodando, instale as dependências de desenvolvimento e rode os testes em um Mac com Google Chrome instalado.

```sh
npm ci
npm run test:studio
npm run test:flow
npm run test:pt
npm run test:tutor
```

Com ElevenLabs configurado, `python3 voice-v4.test.py` verifica a síntese real nos dois modos. Para usar outras portas locais, configure `MEMOBOOK_PORT` (servidor e testes) e `MEMOBOOK_MODEL_PORT` (modelo). Os padrões são 4827 e 4828.

## Licenças

Código da implementação sob [MIT](LICENSE). Os ícones Lucide trazem suas atribuições e licenças no [sprite](public/icons.svg). Os pesos dos modelos são baixados separadamente e têm licenças próprias. A publicação usa tipografia do sistema e um ícone de livro.
