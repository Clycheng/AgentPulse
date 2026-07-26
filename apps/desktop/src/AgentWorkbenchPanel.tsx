type WorkbenchTask = {
  id: string;
  title: string;
  workflow_status?: string;
  waiting_reason?: string;
  story_points?: number;
  priority_score?: number;
  dynamic_priority_score?: number;
  blockers?: Array<{
    id: string;
    dependency_type?: string;
    dependency_legacy_status?: string;
  }>;
};

export type AgentWorkbench = {
  state: {
    activity: string;
    pending_request_count: number;
    queue_depth: number;
  };
  current_task: WorkbenchTask | null;
  requests: Array<{
    id: string;
    status: string;
    content: string;
    requester_type: string;
    story_points: number | null;
    priority_score: number;
  }>;
  queue: WorkbenchTask[];
  paused: WorkbenchTask[];
  resources: Array<{
    id: string;
    resource_type: string;
    resource_key: string;
    mode: string;
  }>;
};

const activityLabel: Record<string, string> = {
  available: '可接新工作',
  triaging: '正在评估请求',
  focused: '专注执行中',
  waiting: '等待依赖',
  blocked: '已阻塞',
  degraded: '运行降级',
};

const requestLabel: Record<string, string> = {
  delivered: '已送达',
  acknowledged: '已知悉',
  evaluating: '评估中',
  answered: '已回答',
  accepted: '已接受',
  deferred: '已排队',
  rejected: '已拒绝',
  needs_info: '待补充',
};

export function AgentWorkbenchPanel({
  workbench,
}: {
  workbench: AgentWorkbench | null;
}) {
  if (!workbench) return null;
  const score = (task: WorkbenchTask) =>
    Number(task.dynamic_priority_score ?? task.priority_score ?? 0).toFixed(1);

  return (
    <section className="drawer-section agent-workbench">
      <div className="agent-workbench-head">
        <h3>工作台</h3>
        <span data-activity={workbench.state.activity}>
          {activityLabel[workbench.state.activity] ?? workbench.state.activity}
        </span>
      </div>

      <div className="agent-workbench-counts">
        <div><strong>{workbench.state.pending_request_count}</strong><span>待评估请求</span></div>
        <div><strong>{workbench.state.queue_depth}</strong><span>个人队列</span></div>
        <div><strong>{workbench.paused.length}</strong><span>暂停任务</span></div>
      </div>

      {workbench.current_task && (
        <div className="agent-current-work">
          <span>当前工作</span>
          <strong>{workbench.current_task.title}</strong>
          <em>{workbench.current_task.story_points ?? 3} SP</em>
        </div>
      )}

      {workbench.requests.length > 0 && (
        <div className="agent-workbench-list">
          <h4>请求收件箱</h4>
          {workbench.requests.slice(0, 6).map((request) => (
            <div key={request.id}>
              <p>{request.content}</p>
              <span>{requestLabel[request.status] ?? request.status}</span>
            </div>
          ))}
        </div>
      )}

      <div className="agent-workbench-list">
        <h4>任务队列</h4>
        {workbench.queue.slice(0, 8).map((task) => (
          <div key={task.id}>
            <p>
              <strong>{task.title}</strong>
              {task.waiting_reason && <em>正在等：{task.waiting_reason}</em>}
            </p>
            <span>{task.story_points ?? 3} SP · {score(task)}</span>
          </div>
        ))}
        {workbench.queue.length === 0 && <p className="agent-workbench-empty">队列为空</p>}
      </div>

      {workbench.resources.length > 0 && (
        <div className="agent-resource-strip">
          {workbench.resources.map((resource) => (
            <span key={resource.id} title={resource.resource_key}>
              {resource.resource_type} · {resource.mode}
            </span>
          ))}
        </div>
      )}
    </section>
  );
}
