import test from 'node:test'
import assert from 'node:assert/strict'
import { taskProgress, taskIteration } from '../src/utils/taskDisplay.js'

test('continuous task displays cumulative progress without a fake ceiling', () => {
  const task = { continuous_mode: true, progress_current: 12, daily_goal: 4,
    current_iteration: 101, max_iterations: 10 }
  assert.equal(taskProgress(task), 'PASS 12 · 持续运行')
  assert.equal(taskIteration(task), '101 · 持续运行')
})

test('finite task keeps its configured goal display', () => {
  const task = { continuous_mode: false, progress_current: 2, daily_goal: 4,
    current_iteration: 3, max_iterations: 10 }
  assert.equal(taskProgress(task), '2 / 4')
  assert.equal(taskIteration(task), '3 / 10')
})
