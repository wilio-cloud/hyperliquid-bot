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

# Executa el bot d'arbitratge delta-neutral en segon pla
CMD ["python", "main.py", "--mode", "arbitrage", "--headless"]
