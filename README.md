# Radar de Certificações

Robô que roda **todo dia às 08:00 (horário de Brasília), de graça, no GitHub Actions** e:

- lê os preços oficiais das provas da Linux Foundation (CKA, LFCS, PCA) e o banner de promoção do site;
- lê os cupons ativos da Linux Foundation em um agregador atualizado quase todo dia;
- lê preço e desconto dos MBAs da Impacta e dos planos do Cambly;
- converte dólar para real com IOF;
- compara com o dia anterior e aponta **o que mudou** (preço novo, cupom novo, texto de promoção novo);
- publica um relatório completo em uma página (GitHub Pages) e manda um resumo no **Telegram** e/ou **e-mail**.

O relatório também traz, para cada certificação: formato da prova, duração, nota de aprovação, validade,
tempo de estudo estimado para o seu perfil, lacunas prováveis, pontos de atenção e material de estudo.
Esses textos ficam em [catalogo.yaml](catalogo.yaml) e você pode editar à vontade.

## Custo

| Item | Custo |
|---|---|
| GitHub Actions | Grátis. Repositório público: ilimitado. Privado: 2.000 min/mês, e o robô usa ~1 min/dia |
| GitHub Pages | Grátis em repositório **público**. Em repositório privado só no plano pago |
| Telegram | Grátis |
| E-mail (Gmail) | Grátis, com senha de app |
| Cotação do dólar (open.er-api.com) | Grátis, sem cadastro |

**Recomendação:** repositório público. Não há segredo no código: token do Telegram e senha do e-mail ficam
em *Secrets* do GitHub, que ninguém vê. Se preferir privado, funciona igual, só sem a página (o e-mail leva o relatório completo).

## Passo a passo

### 1. Criar o repositório e subir o código

Crie um repositório vazio em github.com (`preco_estudos`, já criado) e, nesta pasta:

```bash
git init -b main
```

```bash
git add . && git commit -m "Radar de certificações"
```

```bash
git remote add origin https://github.com/Davidleoc/preco_estudos.git
```

```bash
git push -u origin main
```

### 2. Permitir que o robô salve o estado

*Settings → Actions → General → Workflow permissions →* **Read and write permissions** → Save.

### 3. Ligar a página (GitHub Pages)

*Settings → Pages → Source:* **Deploy from a branch** → Branch `main`, pasta `/docs` → Save.
A página fica em `https://davidleoc.github.io/preco_estudos/`.

### 4. Configurar o Telegram (recomendado, chega no celular)

1. No Telegram, fale com **@BotFather**, mande `/newbot` e siga as instruções. Ele te dá um **token**.
2. Mande qualquer mensagem para o seu bot novo.
3. Abra `https://api.telegram.org/bot<TOKEN>/getUpdates` no navegador e copie o número em `"chat":{"id": ...}`.
4. No GitHub: *Settings → Secrets and variables → Actions → New repository secret*:
   - `TELEGRAM_TOKEN` = token do BotFather
   - `TELEGRAM_CHAT_ID` = número do passo 3

### 5. Configurar e-mail (opcional)

Com Gmail, crie uma *senha de app* em myaccount.google.com → Segurança → Senhas de app (exige verificação em 2 etapas). Secrets:

| Secret | Valor |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | seu Gmail |
| `SMTP_PASS` | a senha de app (não a senha normal) |
| `EMAIL_TO` | para onde mandar (pode ser mais de um, separado por vírgula) |

### 6. Testar

*Actions → Radar diário → Run workflow*. Em ~1 minuto chega a mensagem.

### Opcional: só avisar quando mudar algo

*Settings → Secrets and variables → Actions → Variables →* `NOTIFICAR_SO_MUDANCAS` = `true`.

## Rodar no seu computador

```bash
pip install -r requirements.txt
```

```bash
python radar.py --sem-envio
```

O relatório sai em `docs/index.html`.

## Adicionar ou mudar o que é monitorado

Tudo fica em [catalogo.yaml](catalogo.yaml). Para monitorar um preço novo, adicione uma fonte com um regex:

```yaml
    fontes:
      - url: https://site.com/curso
        campos:
          preco_brl: 'R\$\s*([\d.,]+)'   # o que estiver entre () vira o valor
```

Se o site mudar e o regex parar de achar o valor, o relatório mostra "não encontrado" em laranja.

## Limitações (leia)

- **Red Hat, HashiCorp e MongoDB** não mostram preço no HTML da página, então o radar usa um valor de referência
  (marcado como "ref.") e só acompanha textos de promoção. Confirme o preço no checkout.
- **italki** bloqueia robôs; ficou só com informações fixas.
- **Udemy e Quero Bolsa** também bloqueiam robôs.
- Os cupons vêm de um agregador da comunidade (techiescamp). Confirme o desconto no checkout antes de pagar.
- O GitHub pausa agendamentos de repositórios públicos sem atividade por 60 dias. O commit diário do robô conta como atividade.
  Se mesmo assim pausar, basta clicar em *Enable workflow* na aba Actions.
- O horário do agendamento do GitHub pode atrasar alguns minutos em dias de muito uso.
