FROM python:3.7-alpine

WORKDIR /app

COPY *requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

CMD [ "python", "dashboarder.py"]
EXPOSE 5000
LABEL name=dashboarder version=dev
