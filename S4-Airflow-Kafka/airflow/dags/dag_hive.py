from airflow import DAG
from airflow.providers.apache.hive.operators.hive import HiveOperator
from airflow.providers.apache.hive.hooks.hive import HiveServer2Hook
from airflow.operators.python import PythonOperator
from airflow.providers.apache.hdfs.sensors.web_hdfs import WebHdfsSensor
from datetime import datetime, timedelta
import logging

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuración de la DAG
default_args = {
    "owner": "airflow",
    "depends_on_past": False,
    "start_date": datetime(2025, 3, 18),
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}

dag = DAG(
    "dag_hive",
    default_args=default_args,
    schedule_interval=None,  # Se ejecuta manualmente
    catchup=False,
)

# 0 Tarea: Verificar la existencia del archivo CSV en HDFS
check_hdfs_file = WebHdfsSensor(
    task_id="check_hdfs_file",
    filepath="/user/airflow/clientes.csv",
    webhdfs_conn_id="hdfs_default",
    poke_interval=10,  # Revisa cada 10 segundos
    timeout=60,  # Espera máximo 1 minuto antes de fallar
    dag=dag,
)

# 0.1 Tarea: Crear la base de datos en Hive
create_hive_database = HiveOperator(
    task_id="create_hive_database",
    hql="CREATE DATABASE IF NOT EXISTS tienda;",
    hive_cli_conn_id="hive_default",
    dag=dag,
)

# 1 Tarea: Crear la tabla en Hive
create_hive_table = HiveOperator(
    task_id="create_hive_table",
    hql="""
        CREATE EXTERNAL TABLE IF NOT EXISTS tienda.clientes (
            id INT,
            nombre STRING,
            email STRING
        )
        ROW FORMAT DELIMITED
        FIELDS TERMINATED BY ','
        STORED AS TEXTFILE
        LOCATION '/data/clientes/';
    """,
    hive_cli_conn_id="hive_default",
    dag=dag,
)

# 2 Tarea: Cargar datos en Hive desde HDFS
load_data_into_hive = HiveOperator(
    task_id="load_data_into_hive",
    hql="LOAD DATA INPATH '/user/airflow/clientes.csv' INTO TABLE tienda.clientes;",
    hive_cli_conn_id="hive_default",
    dag=dag,
)

# 3 Tarea: Leer datos desde Hive y mostrar en los logs
def query_hive():
    try:
        hive_hook = HiveServer2Hook(hive_cli_conn_id="hive_default")
        conn = hive_hook.get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tienda.clientes LIMIT 5;")
        rows = cursor.fetchall()

        logger.info("📌 Resultados de Hive:")
        for row in rows:
            logger.info(row)

    except Exception as e:
        logger.error(f"Error al consultar Hive: {e}")
        raise

query_hive_task = PythonOperator(
    task_id="query_hive",
    python_callable=query_hive,
    dag=dag,
)

# 🔗 Definir el flujo de tareas
check_hdfs_file >> create_hive_database >> create_hive_table >> load_data_into_hive >> query_hive_task