from confluent_kafka import Producer
import pandas as pd
import json
import time
from config.kafka_config import kafka_config

def delivery_report(err, msg):
    if err is not None:
        print(f"Error in delivery: {err}")
    else:
        print(f"Message delivered to {msg.topic()} [{msg.partition()}]: {msg.value().decode('utf-8')}")

def read_csv_and_produce(file_path):
    df = pd.read_csv(file_path)
    producer = Producer(kafka_config)  # Usamos la configuración de Kafka

    for index, row in df.iterrows():
        record = row.to_dict() # Convertimos la fila a un diccionario
        message = json.dumps(record)
        producer.produce('transactions', message.encode('utf-8'), callback=delivery_report)
        producer.poll(0)  # Sirve para activar el callback
        time.sleep(1)

# Esperar a que se entreguen todos los mensajes pendientes
    producer.flush()

    print("Mensajes enviados correctamente al topic 'transactions'.")

if __name__ == "__main__":
    read_csv_and_produce('../../../datasets/E2/amazon.csv/amazon.csv')
