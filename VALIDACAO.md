# Validação

## O que foi verificado neste ambiente

Ambiente de desenvolvimento: Linux, Python 3.13, aiortc 1.15, Chromium 141 (Playwright),
**sem webcam, sem placa de som e com uma única máquina**. Resultado: 30 testes automatizados
aprovados (`python -m pytest`).

| Item | Como | Resultado |
|---|---|---|
| Inicialização do backend | `python -m teleop` com HTTPS + `/api/health`; testes com uvicorn | OK |
| Autenticação | senha errada → 401; 5 falhas → 429; cookie HttpOnly/SameSite; logout revoga o token; WS sem login → 4401; origem estranha → 4403 | OK |
| Negociação WebRTC | aiortc ↔ aiortc e Chromium ↔ aiortc | OK |
| Vídeo a 5 FPS | captura medida 4,3–5,7 FPS (teste) e 5,0 FPS (Chromium); recepção 5,0 FPS | OK |
| Áudio robô → operador | tom do microfone simulado recebido continuamente no navegador | OK |
| Áudio operador → robô | microfone simulado do Chromium chega ao alto-falante simulado | OK |
| Latência de vídeo fim a fim | 25–35 ms medidos em `localhost` (mesma máquina, sem rede real) | OK (não representa a rede local) |
| Início/encerramento | dispositivos abertos ao iniciar e liberados ao encerrar (verificado no servidor) | OK |
| Dispositivo indisponível | sem webcam/placa de som reais: estados "Indisponível", vídeo e mídia seguem funcionando | OK |
| Perda e recuperação | queda do WebSocket (retoma a mesma sessão) e da conexão WebRTC (renegocia sozinho) | OK |
| Sessão órfã | recarregar a página → "Assumir sessão" sem reabrir a webcam; sem retorno → liberada após a tolerância | OK |

**Não foi testado com hardware real** (webcam USB, microfone, alto-falante) **nem entre dois
computadores numa rede local.** Faça o procedimento abaixo antes de considerar os requisitos
de latência validados.

## Procedimento de validação física

Material: computador do robô com webcam, microfone e alto-falante; computador do operador com
fone de ouvido e microfone; ambos na mesma rede local cabeada ou Wi-Fi estável.

1. **Preparação.** No robô: `python -m sounddevice` (confirme os dispositivos), configure o
   `.env`, gere o certificado e rode `python -m teleop`. No operador: abra
   `https://<IP-do-robô>:8443` no Chrome e aceite o certificado.
2. **Autenticação.** Tente uma senha errada (deve recusar) e depois a correta.
3. **RF01/RF03 — vídeo.** Inicie a sessão. O HUD deve mostrar "Captura 5 FPS configurado,
   ≈5,0 medido" e "Recebido ≈5,0 FPS". Mova a mão diante da webcam.
4. **RF02/RF03 — áudio do robô.** Fale perto do robô; o medidor "Áudio do robô" deve reagir e
   o som sair no fone do operador. Teste "Silenciar" e o volume.
5. **RF04 — voz do operador.** Clique no microfone (ou tecla M), autorize o navegador e fale.
   O som deve sair no alto-falante do robô e o dispositivo mostrar "Reproduzindo sua voz".
   Negue a permissão uma vez para conferir a mensagem orientativa.
6. **RF05 — latência fim a fim (meta < 300 ms).** Anote o valor de "Vídeo fim a fim" durante
   2 minutos (mín./mediana/máx.). Medição independente: filme com um celular, num único
   quadro, um cronômetro em milissegundos na frente da webcam do robô **e** a tela do
   operador exibindo esse cronômetro; a diferença entre os dois valores é a latência visual.
   Repita 10 vezes. Lembre que a 5 FPS cada quadro dura 200 ms, então a latência percebida
   varia até 200 ms além da latência de transmissão.
7. **Latência do áudio.** Bata palmas perto do robô com o celular gravando os dois ambientes
   (ou use uma chamada de vídeo paralela) e meça o intervalo na forma de onda.
8. **Falhas.** (a) Desconecte o cabo de rede do operador por 5 s e reconecte: a interface
   deve mostrar "Tentando reconectar" e voltar sozinha. (b) Desconecte por mais de 20 s: o
   robô encerra a sessão e libera os dispositivos. (c) Desconecte a webcam USB durante a
   sessão: "Webcam do robô indisponível" e retorno automático ao reconectá-la.
9. **RF06 — encerramento.** Encerre a sessão e confirme no robô que o LED da webcam apagou e
   que o log mostra "dispositivos liberados".

Registre os resultados na tabela:

| Item | Data | Rede | Resultado / valores |
|---|---|---|---|
| Vídeo fim a fim (interface) | | | |
| Vídeo fim a fim (cronômetro filmado) | | | |
| Áudio robô → operador | | | |
| Voz operador → robô | | | |
| Perda de pacotes (vídeo/áudio/voz) | | | |
| Reconexão < 20 s | | | |
| Liberação ao encerrar | | | |
