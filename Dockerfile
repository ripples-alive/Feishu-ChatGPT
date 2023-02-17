FROM python:3

WORKDIR /src

COPY requirements.txt /
RUN pip install -r /requirements.txt

COPY src /src

CMD ["python", "-u", "./bot.py"]
