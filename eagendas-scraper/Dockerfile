FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scraper.py app.py ./

# Interface web (Streamlit)
EXPOSE 8501

HEALTHCHECK CMD curl --fail http://localhost:8501/_stcore/health || exit 1

# Por padrão sobe a interface web; para CLI use:
#   docker run --rm -v $(pwd)/dados:/app/dados eagendas python scraper.py --govbr-url "..."
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501"]
