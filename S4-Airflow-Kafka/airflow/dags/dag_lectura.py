from airflow import DAG
from airflow.operators.python import PythonOperator
from hdfs import InsecureClient
from datetime import datetime, timedelta
import pandas as pd
import json
import csv
from confluent_kafka import Producer, Consumer, KafkaException
from airflow.providers.apache.hive.hooks.hive import HiveServer2Hook
from airflow.providers.apache.kafka.operators.produce import ProduceToTopicOperator
import time
import logging
import requests


# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

KAFKA_CONNECT_URL = "http://kafka-connect:8083/connectors"


# Configuración básica de la DAG
default_args = {
    "owner": "airflow",
    "depends_on_past": False,
    "start_date": datetime(2025, 3, 19),
    "retries": 1,
    "retry_delay": timedelta(minutes=1),
}

dag = DAG(
    "dag_csv_to_kafka",
    default_args=default_args,
    schedule_interval=None,  # Se ejecuta manualmente
    catchup=False,
)

def delivery_report(err, msg):
    if err is not None:
        print(f"Error in delivery: {err}")
    else:
        print(f"Message delivered to {msg.topic()} [{msg.partition()}]: {msg.value().decode('utf-8')}")


# Función para leer el archivo CSV, convertir a JSON y publicar en Kafka
def read_csv_and_produce():
    messages = []
    csv_path = "/opt/airflow/dataset/home_temperature_and_humidity_smoothed_filled.csv"

    try:
        with open(csv_path, 'r') as file:
            reader = csv.DictReader(file)
            for row in reader:
                key = row['timestamp']  # Usamos timestamp como clave
                value = json.dumps(row)  # Convertimos toda la fila a JSON
                messages.append((key, value))
        return messages
    except FileNotFoundError:
        print(f" ERROR: El archivo {csv_path} no se encuentra en el contenedor.")
        raise
    except Exception as e:
        print(f" ERROR inesperado: {str(e)}")
        raise


# Función para verificar que los mensajes han sido entregados a Kafka
def check_kafka_messages():
    consumer = Consumer({
        'bootstrap.servers': 'kafka:9092',
        'group.id': 'airflow-consumer',
        'auto.offset.reset': 'earliest'  # Comienza desde el inicio del topic
    })

    consumer.subscribe(["sensores"])
    message_count = 0

    try:
        for _ in range(5):  # Intentamos consumir 5 mensajes
            msg = consumer.poll(timeout=1.0)  # Tiempo de espera para recibir un mensaje

            if msg is None:
                logger.warning("No message received within the timeout period.")
            elif msg.error():
                raise KafkaException(msg.error())
            else:
                logger.info(f"Message received: {msg.value().decode('utf-8')}")
                message_count += 1

        if message_count > 0:
            logger.info(f"Successfully received {message_count} messages from Kafka.")
        else:
            logger.error("No messages were received from Kafka.")
    finally:
        consumer.close()


# Archivo JSON para la configuración del conector HDFS
HDFS_CONNECTOR_CONFIG = {
    "name": "hdfs-sink-connector",
    "config": {
        "connector.class": "io.confluent.connect.hdfs.HdfsSinkConnector",
        "tasks.max": "1",
        "hdfs.url": "hdfs://namenode:9000",
        "flush.size": "10",
        "format.class": "io.confluent.connect.hdfs.json.JsonFormat",
        "partitioner.class": "io.confluent.connect.storage.partitioner.DefaultPartitioner",
        "topics.dir": "/topics",
        "topics": "sensores",
        "rotate.interval.ms": "60000",
        "locale": "en",
        "timezone": "UTC",
        "value.converter.schemas.enable": "false",
        "hdfs.authentication.kerberos": "false"
    }
}

# Función para registrar el conector HDFS en Kafka Connect a través de la API
def register_hdfs_connector():
    headers = {'Content-Type': 'application/json'}
    
    # Verifica si el conector ya existe
    check_response = requests.get(f"{KAFKA_CONNECT_URL}/hdfs-sink-connector", headers=headers)
    if check_response.status_code == 200:
        print("El conector HDFS ya está registrado. No es necesario crearlo de nuevo.")
        return
    
    response = requests.post(KAFKA_CONNECT_URL, headers=headers, data=json.dumps(HDFS_CONNECTOR_CONFIG))

    if response.status_code == 201:
        print("Conector HDFS registrado correctamente.")
    else:
        print(f"Error al registrar el conector HDFS: {response.status_code} - {response.text}")


# Función para verificar que los archivos JSON estén almacenados en HDFS
def verify_files_in_hdfs():
    # Crear el cliente HDFS
    hdfs_client = InsecureClient('http://namenode:9870')  # Cambia esto según tu configuración HDFS

    directorio_hdfs = '/topics/sensores/partition=0/'  # Directorio donde se almacenan los archivos

    archivos_json = []
    for _ in range(30):  # Intentar 30 veces (1 minuto) con 2 segundos de intervalo entre intentos
        try:
            archivos = hdfs_client.list(directorio_hdfs)  # Listar los archivos en el directorio HDFS
            archivos_json = [archivo for archivo in archivos if archivo.endswith('.json')]  # Filtrar archivos JSON

            if archivos_json:  # Si se encontraron archivos .json, terminamos el ciclo
                break
        except Exception as e:
            print(f"Error al listar archivos en HDFS: {e}")
        time.sleep(2)  # Esperar 2 segundos antes de volver a intentar

    if not archivos_json:
        raise Exception(f"No se encontraron archivos JSON en el directorio {directorio_hdfs} en HDFS.")

    # Si encontramos archivos JSON, solo verificamos su existencia
    print(f"Archivos JSON encontrados en {directorio_hdfs}:")
    for archivo in archivos_json:
        print(f"{archivo}\n")


# Función para crear la base de datos en Hive
def create_database():
    try:
        hive_hook = HiveServer2Hook(hiveserver2_conn_id="hive_default")
        conn = hive_hook.get_conn()
        cursor = conn.cursor()
        cursor.execute("CREATE DATABASE IF NOT EXISTS sensores")
        cursor.close()
        conn.close()
        logger.info("✅ Base de datos creada correctamente en Hive")
    except Exception as e:
        logger.error(f"❌ Error al crear la base de datos en Hive: {e}")
        raise

# Función para crear la tabla en Hive
def create_table():
    try:
        hive_hook = HiveServer2Hook(hiveserver2_conn_id="hive_default")
        conn = hive_hook.get_conn()
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sensores.sensores_info (
                `timestamp` TIMESTAMP,
                `temperature_salon` DOUBLE,
                `humidity_salon` DOUBLE,
                `air_salon` DOUBLE,
                `temperature_chambre` DOUBLE,
                `humidity_chambre` DOUBLE,
                `air_chambre` DOUBLE,
                `temperature_bureau` DOUBLE,
                `humidity_bureau` DOUBLE,
                `air_bureau` DOUBLE,
                `temperature_exterieur` DOUBLE,
                `humidity_exterieur` DOUBLE,
                `air_exterieur` DOUBLE
            )
            ROW FORMAT SERDE 'org.apache.hive.hcatalog.data.JsonSerDe'
            WITH SERDEPROPERTIES (
                "serialization.format"="1"
            )
            STORED AS TEXTFILE
            LOCATION 'hdfs://namenode:9000/topics/sensores/partition=0/'
        """)
        cursor.close()
        conn.close()
        logger.info("✅ Tabla creada correctamente en Hive")
    except Exception as e:
        logger.error(f"❌ Error al crear la tabla en Hive: {e}")
        raise


# Función para cargar los datos JSON en Hive desde HDFS
def load_data_into_hive():
    try:
        hive_hook = HiveServer2Hook(hiveserver2_conn_id="hive_default")
        conn = hive_hook.get_conn()
        cursor = conn.cursor()
        
        # Cargar los datos desde HDFS en la tabla
        cursor.execute("""
            LOAD DATA INPATH 'hdfs://namenode:9000/topics/sensores/partition=0/'
            INTO TABLE sensores
        """)
        cursor.close()
        conn.close()
        logger.info("✅ Datos cargados correctamente en la tabla 'sensores' en Hive")
    except Exception as e:
        logger.error(f"❌ Error al cargar los datos en Hive: {e}")
        raise

# Consulta para obtener la temperatura media por ubicación y día
def get_avg_temperature():
    try:
        hive_hook = HiveServer2Hook(hiveserver2_conn_id="hive_default")
        conn = hive_hook.get_conn()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT 
                DATE(`timestamp`) AS day,
                AVG(`temperature_salon`) AS avg_temp_salon,
                AVG(`temperature_chambre`) AS avg_temp_chambre,
                AVG(`temperature_bureau`) AS avg_temp_bureau,
                AVG(`temperature_exterieur`) AS avg_temp_exterieur
            FROM sensores.sensores_info
            GROUP BY DATE(`timestamp`)
        """)
        results = cursor.fetchall()
        logger.info(f"✅ Temperatura media por ubicación y día: {results}")

        cursor.close()
        conn.close()
    except Exception as e:
        logger.error(f"❌ Error al obtener la temperatura media: {e}")
        raise

# Consulta para detectar los momentos de peor calidad del aire
def get_worst_air_quality():
    try:
        hive_hook = HiveServer2Hook(hiveserver2_conn_id="hive_default")
        conn = hive_hook.get_conn()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT `timestamp`, `air_salon`, `air_chambre`, `air_bureau`, `air_exterieur`
            FROM sensores.sensores_info
            ORDER BY GREATEST(`air_salon`, `air_chambre`, `air_bureau`, `air_exterieur`) DESC
            LIMIT 10
        """)

        results = cursor.fetchall()
        logger.info(f"✅ Peor calidad del aire: {results}")

        cursor.close()
        conn.close()
    except Exception as e:
        logger.error(f"❌ Error al obtener los momentos de peor calidad del aire: {e}")
        raise

# Consulta para detectar variaciones bruscas de humedad
def get_humidity_variations():
    try:
        hive_hook = HiveServer2Hook(hiveserver2_conn_id="hive_default")
        conn = hive_hook.get_conn()
        cursor = conn.cursor()
        cursor.execute("""
            WITH cambio_humedad AS (
                SELECT
                    `timestamp`,
                    humidity_salon,
                    LAG(humidity_salon) OVER (ORDER BY `timestamp`) AS prev_humidity_salon

                FROM
                    sensores.sensores_info
            )
            SELECT
               `timestamp`,
                COALESCE(humidity_salon, 0) AS humidity_salon,
                COALESCE(prev_humidity_salon, 0) AS prev_humidity_salon,
                CASE 
                    WHEN prev_humidity_salon = 0 THEN NULL
                    ELSE ABS(humidity_salon - prev_humidity_salon) / prev_humidity_salon * 100
                END AS humidity_variation_percentage
            FROM
                humedad_change
            WHERE
                prev_humidity_salon != 0 AND ABS(humidity_salon - prev_humidity_salon) / prev_humidity_salon * 100 > 10
            ORDER BY
                `timestamp`

        """)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        print("✅ Resultados de Hive:")
        for row in rows:
            print(row)
    except Exception as e:
        print(f"❌ Error al consultar Hive: {str(e)}")
        raise

# Tarea para leer el csv y publicar en Kafka
produce_task = ProduceToTopicOperator(
    task_id='produce_to_kafka',
    topic="sensores",
    kafka_config_id="kafka_default",
    producer_function=read_csv_and_produce,
    dag=dag
)

# Tarea para verificar que los mensajes fueron entregados a Kafka
check_task = PythonOperator(
    task_id='check_kafka_messages',
    python_callable=check_kafka_messages,
    dag=dag,
)

register_hdfs_task = PythonOperator(
    task_id='register_hdfs_connector',
    python_callable=register_hdfs_connector,
    dag=dag,
)

# Tarea para verificar que los archivos JSON estén en HDFS
verify_files_task = PythonOperator(
    task_id='verify_files_in_hdfs',
    python_callable=verify_files_in_hdfs,
    dag=dag,
)

# Tarea para crear la BD en Hive
create_database_task = PythonOperator(
    task_id="create_hive_database",
    python_callable=create_database,
    dag=dag,
)

# Tarea para crear la tabla en Hive
create_table_task = PythonOperator(
    task_id="create_hive_table",
    python_callable=create_table,
    dag=dag,
)

# Tarea para cargar los datos en Hive
load_data_task = PythonOperator(
    task_id="load_data_into_hive",
    python_callable=load_data_into_hive,
    dag=dag,
)

# Tarea para obtener la temperatura media por ubicación y día
avg_temperature_task = PythonOperator(
    task_id="get_avg_temperature",
    python_callable=get_avg_temperature,
    dag=dag,
)

# Tarea para detectar los momentos de peor calidad del aire
worst_air_quality_task = PythonOperator(
    task_id="get_worst_air_quality",
    python_callable=get_worst_air_quality,
    dag=dag,
)

# Tarea para detectar variaciones bruscas de humedad
humidity_variations_task = PythonOperator(
    task_id="get_humidity_variations",
    python_callable=get_humidity_variations,
    dag=dag,
)

produce_task >> check_task >> register_hdfs_task >> verify_files_task >> create_database_task >> create_table_task >> load_data_task >> avg_temperature_task >> worst_air_quality_task >> humidity_variations_task
