"""Prometheus exporter for Airflow."""

from contextlib import contextmanager

from airflow.models import DagModel, DagRun, TaskInstance, TaskFail
from airflow.plugins_manager import AirflowPlugin
from airflow.settings import Session
from airflow.utils.state import State
from flask import Response
from flask_admin import BaseView, expose
from prometheus_client import generate_latest, REGISTRY
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import and_, func

CANARY_DAG = 'canary_dag'


@contextmanager
def session_scope(session):
    """Provide a transactional scope around a series of operations."""
    try:
        yield session
    finally:
        session.close()


######################
# DAG Related Metrics
######################

def get_dag_state_info():
    """Number of DAG Runs with particular state."""
    with session_scope(Session) as session:
        dag_status_query = session.query(
            DagRun.dag_id,
            DagRun.state,
            func.count(DagRun.state).label('count')
        ).group_by(DagRun.dag_id, DagRun.state).subquery()
        return session.query(
            dag_status_query.c.dag_id,
            dag_status_query.c.state,
            dag_status_query.c.count,
            DagModel.owners
        ).join(
            DagModel,
            DagModel.dag_id == dag_status_query.c.dag_id
        ).all()


def get_dag_duration_info():
    """Duration of successful DAG Runs."""
    with session_scope(Session) as session:
        max_execution_dt_query = session.query(
            DagRun.dag_id,
            func.max(DagRun.execution_date).label('max_execution_dt')
        ).filter(
            DagRun.state == State.SUCCESS,
            DagRun.end_date.isnot(None),
        ).group_by(
            DagRun.dag_id
        ).subquery()

        dag_start_dt_query = session.query(
            max_execution_dt_query.c.dag_id,
            max_execution_dt_query.c.max_execution_dt.label('execution_date'),
            func.min(TaskInstance.start_date).label('start_date')
        ).join(
            TaskInstance,
            and_(
                TaskInstance.dag_id == max_execution_dt_query.c.dag_id,
                (
                    TaskInstance.execution_date
                    ==
                    max_execution_dt_query.c.max_execution_dt
                )
            )
        ).group_by(
            max_execution_dt_query.c.dag_id,
            max_execution_dt_query.c.max_execution_dt,
        ).subquery()

        return session.query(
            dag_start_dt_query.c.dag_id,
            dag_start_dt_query.c.start_date,
            DagRun.end_date,
        ).join(
            DagRun,
            and_(
                DagRun.dag_id == dag_start_dt_query.c.dag_id,
                DagRun.execution_date == dag_start_dt_query.c.execution_date
            )
        ).all()

######################
# Task Related Metrics
######################


def get_task_state_info():
    """Number of task instances with particular state."""
    with session_scope(Session) as session:
        task_status_query = session.query(
            TaskInstance.dag_id,
            TaskInstance.task_id,
            TaskInstance.state,
            func.count(TaskInstance.dag_id).label('value')
        ).group_by(
            TaskInstance.dag_id,
            TaskInstance.task_id,
            TaskInstance.state
        ).subquery()
        return session.query(
            task_status_query.c.dag_id,
            task_status_query.c.task_id,
            task_status_query.c.state,
            task_status_query.c.value, DagModel.owners
        ).join(
            DagModel,
            DagModel.dag_id == task_status_query.c.dag_id
        ).all()


def get_task_failure_counts():
    """Compute Task Failure Counts."""
    with session_scope(Session) as session:
        return session.query(
            TaskFail.dag_id,
            TaskFail.task_id,
            func.count(TaskFail.dag_id).label('count')
        ).group_by(
            TaskFail.dag_id,
            TaskFail.task_id,
        )


def get_task_duration_info():
    """Duration of successful tasks in seconds."""
    with session_scope(Session) as session:
        max_execution_dt_query = session.query(
            DagRun.dag_id,
            func.max(DagRun.execution_date).label('max_execution_dt')
        ).filter(
            DagRun.state == State.SUCCESS,
            DagRun.end_date.isnot(None),
        ).group_by(
            DagRun.dag_id
        ).subquery()

        task_duration_query = session.query(
            TaskInstance.dag_id,
            TaskInstance.task_id,
            func.max(TaskInstance.execution_date).label('max_execution_dt')
        ).filter(
            TaskInstance.state == State.SUCCESS,
            TaskInstance.start_date.isnot(None),
            TaskInstance.end_date.isnot(None),
        ).group_by(
            TaskInstance.dag_id,
            TaskInstance.task_id
        ).subquery()

        task_latest_execution_dt = session.query(
            task_duration_query.c.dag_id,
            task_duration_query.c.task_id,
            task_duration_query.c.max_execution_dt.label('execution_date'),
        ).join(
            max_execution_dt_query,
            and_(
                (
                    task_duration_query.c.dag_id
                    ==
                    max_execution_dt_query.c.dag_id
                ),
                (
                    task_duration_query.c.max_execution_dt
                    ==
                    max_execution_dt_query.c.max_execution_dt
                ),
            )
        ).subquery()

        return session.query(
            task_latest_execution_dt.c.dag_id,
            task_latest_execution_dt.c.task_id,
            TaskInstance.start_date,
            TaskInstance.end_date,
            task_latest_execution_dt.c.execution_date,
        ).join(
            TaskInstance,
            and_(
                TaskInstance.dag_id == task_latest_execution_dt.c.dag_id,
                TaskInstance.task_id == task_latest_execution_dt.c.task_id,
                (
                    TaskInstance.execution_date
                    ==
                    task_latest_execution_dt.c.execution_date
                ),
            )
        ).all()

######################
# Scheduler Related Metrics
######################


def get_dag_scheduler_delay():
    """Compute DAG scheduling delay."""
    with session_scope(Session) as session:
        return session.query(
            DagRun.dag_id,
            DagRun.execution_date,
            DagRun.start_date,
        ).filter(
            DagRun.dag_id == CANARY_DAG,
        ).order_by(
            DagRun.execution_date.desc()
        ).limit(1).all()


def get_task_scheduler_delay():
    """Compute Task scheduling delay."""
    with session_scope(Session) as session:
        task_status_query = session.query(
            TaskInstance.queue,
            func.max(TaskInstance.start_date).label('max_start'),
        ).filter(
            TaskInstance.dag_id == CANARY_DAG,
            TaskInstance.queued_dttm.isnot(None),
        ).group_by(
            TaskInstance.queue
        ).subquery()
        return session.query(
            task_status_query.c.queue,
            TaskInstance.execution_date,
            TaskInstance.queued_dttm,
            task_status_query.c.max_start.label('start_date'),
        ).join(
            TaskInstance,
            and_(
                TaskInstance.queue == task_status_query.c.queue,
                TaskInstance.start_date == task_status_query.c.max_start,
            )
        ).all()


def get_num_queued_tasks():
    """Number of queued tasks currently."""
    with session_scope(Session) as session:
        return session.query(
            TaskInstance
        ).filter(
            TaskInstance.state == State.QUEUED
        ).count()


class MetricsCollector(object):
    """Metrics Collector for prometheus."""

    def describe(self):
        return []

    def collect(self):
        """Collect metrics."""
        # Task metrics
        task_info = get_task_state_info()
        t_state = GaugeMetricFamily(
            'airflow_task_status',
            'Shows the number of task instances with particular status',
            labels=['dag_id', 'task_id', 'owner', 'status']
        )
        for task in task_info:
            t_state.add_metric(
                [task.dag_id, task.task_id, task.owners, task.state or 'none'],
                task.value
            )
        yield t_state

        task_duration = GaugeMetricFamily(
            'airflow_task_duration',
            'Duration of successful tasks in seconds',
            labels=['task_id', 'dag_id', 'execution_date']
        )
        for task in get_task_duration_info():
            task_duration = (task.end_date - task.start_date).total_seconds()
            task_duration.add_metric(
                [task.task_id, task.dag_id, str(task.execution_date.date())],
                task_duration
            )
        yield task_duration

        task_failure_count = GaugeMetricFamily(
            'airflow_task_fail_count',
            'Count of failed tasks',
            labels=['dag_id', 'task_id']
        )
        for task in get_task_failure_counts():
            task_failure_count.add_metric(
                [task.dag_id, task.task_id],
                task.count
            )
        yield task_failure_count

        # Dag Metrics
        dag_info = get_dag_state_info()
        d_state = GaugeMetricFamily(
            'airflow_dag_status',
            'Shows the number of dag starts with this status',
            labels=['dag_id', 'owner', 'status']
        )
        for dag in dag_info:
            d_state.add_metric(
                [dag.dag_id, dag.owners, dag.state],
                dag.count
            )
        yield d_state

        dag_duration = GaugeMetricFamily(
            'airflow_dag_run_duration',
            'Duration of successful dag_runs in seconds',
            labels=['dag_id']
        )
        for dag in get_dag_duration_info():
            dag_duration = (dag.end_date - dag.start_date).total_seconds()
            dag_duration.add_metric(
                [dag.dag_id],
                dag_duration
            )
        yield dag_duration

        # Scheduler Metrics
        dag_scheduler_delay = GaugeMetricFamily(
            'airflow_dag_scheduler_delay',
            'Airflow DAG scheduling delay',
            labels=['dag_id']
        )

        for dag in get_dag_scheduler_delay():
            dag_scheduling_delay = (
                dag.start_date - dag.execution_date).total_seconds()
            dag_scheduler_delay.add_metric(
                [dag.dag_id],
                dag_scheduling_delay
            )
        yield dag_scheduler_delay

        task_scheduler_delay = GaugeMetricFamily(
            'airflow_task_scheduler_delay',
            'Airflow Task scheduling delay',
            labels=['queue']
        )

        for task in get_task_scheduler_delay():
            task_scheduling_delay = (
                task.start_date - task.queued_dttm).total_seconds()
            task_scheduler_delay.add_metric(
                [task.queue],
                task_scheduling_delay
            )
        yield task_scheduler_delay

        num_queued_tasks_metric = GaugeMetricFamily(
            'airflow_num_queued_tasks',
            'Airflow Number of Queued Tasks',
        )

        num_queued_tasks = get_num_queued_tasks()
        num_queued_tasks_metric.add_metric([], num_queued_tasks)
        yield num_queued_tasks_metric


REGISTRY.register(MetricsCollector())


class Metrics(BaseView):
    @expose('/')
    def index(self):
        return Response(generate_latest(), mimetype='text/plain')


ADMIN_VIEW = Metrics(category='Prometheus exporter', name='metrics')


class AirflowPrometheusPlugin(AirflowPlugin):
    """Airflow Pluging for collecting metrics."""

    name = 'airflow_prometheus_plugin'
    operators = []
    hooks = []
    executors = []
    macros = []
    admin_views = [ADMIN_VIEW]
    flask_blueprints = []
    menu_links = []
    appbuilder_views = []
    appbuilder_menu_items = []
