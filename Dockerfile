FROM python:3.12-slim

# Evita que Python emmagatzemi en memòria intermèdia la sortida de la consola
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

WORKDIR /app

# Instal·la dependències
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia el codi del bot
COPY . .

# Port web per veure el tauler des del mòbil / navegador
EXPOSE 8080

# Defaults per al mode funding carry (sobreescrivibles via Railway env vars)
ENV BOT_MODE=funding_carry
ENV MAKER_FIRST=true
ENV MAX_POSITIONS=3
ENV MIN_CARRY_APR=18.0
ENV MIN_EXIT_APR=3.0
ENV CARRY_SLOTS=3

# Executa el bot — el mode es llegeix de la variable d'entorn BOT_MODE
CMD ["sh", "-c", "python main.py --mode ${BOT_MODE} --headless"]
