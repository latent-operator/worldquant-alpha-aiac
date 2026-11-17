"""Offline regression checks for restoring the upstream mining entrypoint."""
import os
os.environ.update(BRAIN_EMAIL='', BRAIN_PASSWORD='', OPENAI_API_KEY='offline',
                  OPENAI_BASE_URL='http://127.0.0.1:1/v1', REDIS_URL='redis://127.0.0.1:1/0')
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
from backend.services.task_service import TaskService
from backend.tasks import mining_tasks as mining


class NativeDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Prevent the retired implementation from touching any service in the red run.
        try:
            from backend.services import bounded_research
        except ImportError:
            return
        self.old = patch.object(bounded_research, 'start', AsyncMock(return_value={'run_id':-1}), create=True)
        self.old.start(); self.addCleanup(self.old.stop)

    async def test_start_uses_native_run_and_queue(self):
        db = AsyncMock()
        service = TaskService(db)
        task = NS(id=90, status='PENDING', config={'target_hypothesis': 'test'},
                  region='USA', universe='TOP3000', dataset_strategy='AUTO',
                  target_datasets=[], daily_goal=1)
        service.task_repo.get_by_id = AsyncMock(return_value=task)
        service.task_repo.update_status = AsyncMock()
        created = NS(id=91)
        service.run_repo.create = AsyncMock(return_value=created)
        with patch('backend.tasks.run_mining_task.delay', return_value=NS(id='offline')) as queue:
            result = await service.start_task(90)
        self.assertEqual(result['run_id'], 91)
        queue.assert_called_once_with(90, 91)
        run = service.run_repo.create.call_args.args[0]
        self.assertEqual(run.trigger_source, 'API')
        self.assertEqual(run.config_snapshot['task']['config'], task.config)

    async def test_retired_task_cannot_start_resume_or_adjust(self):
        for action in ('START', 'RESUME', 'ADJUST'):
            db = AsyncMock(); service = TaskService(db)
            service.task_repo.get_by_id = AsyncMock(return_value=NS(
                id=4, status='PAUSED', config={'execution_mode': 'bounded'}))
            with self.assertRaisesRegex(ValueError, 'retired'):
                if action == 'START': await service.start_task(4)
                else: await service.intervene_task(4, action, {'execution_mode':'native'})
            db.commit.assert_not_awaited()


class NativeWorkerTests(unittest.TestCase):
    def setUp(self):
        try:
            from backend.services import bounded_research
        except ImportError:
            return
        self.old = patch.object(bounded_research, 'execute', AsyncMock(return_value={'status':'old'}), create=True)
        self.old.start(); self.addCleanup(self.old.stop)

    def test_worker_runs_existing_evolution_loop(self):
        self.exercise_worker(False)

    def test_pause_after_native_batch_keeps_results_and_intervention(self):
        self.exercise_worker(True)

    def exercise_worker(self, pause):
        task = NS(id=90, status='RUNNING', config={}, region='USA', universe='TOP3000',
                  progress_current=0, daily_goal=1, max_iterations=3)
        run = NS(id=91, status='RUNNING', error_message=None)
        db = AsyncMock()
        db.execute.return_value = MagicMock(scalar_one_or_none=lambda: task)
        ctx = MagicMock(); ctx.__aenter__ = AsyncMock(return_value=db)
        ctx.__aexit__ = AsyncMock(return_value=False)
        brain = MagicMock(); brain.__aenter__ = AsyncMock(return_value=brain)
        brain.__aexit__ = AsyncMock(return_value=False)
        agent = MagicMock()
        async def evolve(**kwargs):
            if pause: task.status='PAUSED'
            return {'total_success':0,'all_alphas':[],'target_reached':False,'iterations_completed':3}
        agent.run_evolution_loop = AsyncMock(side_effect=evolve)
        with patch.object(mining,'AsyncSessionLocal',return_value=ctx), \
             patch.object(mining,'MCPBrainAdapter',return_value=brain), \
             patch.object(mining,'MiningAgent',return_value=agent), \
             patch.object(mining,'_get_or_create_run',AsyncMock(return_value=run)), \
             patch.object(mining,'_get_datasets_to_mine',AsyncMock(return_value=['pricevolume6'])), \
             patch.object(mining,'_get_operators',AsyncMock(return_value=[])), \
             patch.object(mining,'_get_dataset_fields',AsyncMock(return_value=[{'id':'close'}])), \
             patch('backend.alpha_semantic_validator.load_operators_from_db',AsyncMock(return_value=[])):
            result = mining.run_mining_task.run(90,91)
        agent.run_evolution_loop.assert_awaited_once()
        self.assertEqual(agent.run_evolution_loop.call_args.kwargs['max_iterations'],3)
        self.assertTrue(result['success'])
        self.assertEqual(run.status,'PAUSED' if pause else 'COMPLETED')
        if pause: self.assertIsNone(run.error_message)
        else: self.assertIn('target not reached',run.error_message)

    def test_worker_rejects_retired_queue_delivery_before_mutation(self):
        db = AsyncMock()
        db.execute.return_value = MagicMock(scalar_one_or_none=lambda: NS(
            id=4,status='PAUSED',config={'execution_mode':'bounded'}))
        ctx = MagicMock(); ctx.__aenter__ = AsyncMock(return_value=db)
        ctx.__aexit__ = AsyncMock(return_value=False)
        with patch.object(mining,'AsyncSessionLocal',return_value=ctx), \
             patch.object(mining,'MCPBrainAdapter') as brain:
            result = mining.run_mining_task.run(4,5)
        self.assertIn('retired',result.get('error',''))
        db.commit.assert_not_awaited(); brain.assert_not_called()


if __name__=='__main__': unittest.main()
