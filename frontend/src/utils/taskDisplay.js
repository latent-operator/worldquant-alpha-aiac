export function taskProgress(task) {
  return task?.continuous_mode
    ? `PASS ${task.progress_current || 0} · 持续运行`
    : `${task?.progress_current || 0} / ${task?.daily_goal || 0}`
}

export function taskIteration(task) {
  return task?.continuous_mode
    ? `${task.current_iteration || 0} · 持续运行`
    : `${task?.current_iteration || 0} / ${task?.max_iterations || 1}`
}
