import { Fragment, StrictMode, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode, RefObject } from 'react';
import { createRoot } from 'react-dom/client';
import { useTranslation } from 'react-i18next';
import './i18n';
import './styles.css';
import { getAppLanguage, setAppLanguage } from './i18n';
import type { AppLanguage } from './i18n';
import {
  ModelProviderModal,
  ModelProviderSettings,
} from './ModelProviderSettings';
import type { ModelProviderStatus } from './ModelProviderSettings';

declare global {
  interface Window {
    agentpulse?: {
      platform: string;
      session: {
        get: () => Promise<{ accessToken: string; user: User } | null>;
        set: (value: { accessToken: string; user: User }) => Promise<boolean>;
        clear: () => Promise<boolean>;
      };
      obsidian: {
        pickManaged: () => Promise<{
          vault_name: string;
          managed_area: string;
          documents: Array<{
            relative_path: string;
            title: string;
            content: string;
            modified_at: string;
          }>;
        } | null>;
      };
    };
  }
}

type View =
  | 'chat'
  | 'staff'
  | 'market'
  | 'tasks'
  | 'ideas'
  | 'lib'
  | 'channels'
  | 'settings';
type ThemeMode = 'system' | 'light' | 'dark';
type EffectiveTheme = 'light' | 'dark';
type AgentStatus = 'busy' | 'wait' | 'stuck' | 'idle';
type TaskStatus = '待执行' | '待认领' | '进行中' | '待确认' | '阻塞' | '已完成';
type Priority = 'P0' | 'P1' | 'P2';
type LibraryTab = 'docs' | 'skills' | 'mcp';

type User = {
  id: string;
  email: string;
  display_name: string;
};

type Workspace = {
  id: string;
  name: string;
  onboarding_completed: boolean;
};

type Department = {
  id: string;
  name: string;
  parent_id: string | null;
  sort_order: number;
};

type Agent = {
  id: string;
  name: string;
  role: string;
  description: string;
  dept: string;
  departmentId: string;
  hue: number;
  glyph: string;
  statusKind: AgentStatus;
  statusLabel: string;
  joined: string;
  prompt: string;
  skills: string[];
  mcps: string[];
  experiences: AgentExperience[];
};

type Chat =
  | {
      id: string;
      kind: 'dm';
      agentId: string;
      unread: number;
      time: string;
    }
  | {
      id: string;
      kind: 'group';
      name: string;
      memberIds: string[];
      unread: number;
      time: string;
    };

type Message = {
  id: string;
  from: string;
  type: 'system' | 'text';
  time: string;
  text: string;
  provider?: string;
  model?: string;
};

type TeamMemberDraft = {
  name: string;
  role: string;
  department: string;
  description: string;
  responsibilities: string[];
  capability_keys: string[];
};

type Task = {
  id: string;
  title: string;
  description: string;
  pr: Priority;
  owner: string;
  suggestedAgentId?: string | null;
  suggestedAgentReason?: string;
  status: TaskStatus;
  progress: number;
  src: string;
  srcLabel: string;
  dueDate?: string | null;
  parentTaskId?: string | null;
  taskPlanId?: string | null;
  planItemKey?: string | null;
  expectedOutput: string;
  outputType: string;
  createdAt: string;
  updatedAt: string;
  events: TaskEvent[];
  outputs: TaskOutput[];
  approvals: Approval[];
};

type TaskEvent = {
  id: string;
  taskId: string;
  conversationId: string | null;
  agentId: string | null;
  kind: string;
  title: string;
  content: string;
  time: string;
};

type TaskOutput = {
  id: string;
  taskId: string;
  conversationId: string | null;
  agentId: string | null;
  title: string;
  outputType: string;
  content: string;
  time: string;
};

type TaskPlanSnapshot = {
  id: string;
  brief_id: string;
  root_task_id: string | null;
  status: 'launching' | 'active' | 'blocked' | 'completed' | 'cancelled';
  revision_count: number;
  blocked_reason: string;
  tasks: Array<{
    id: string;
    title: string;
    owner_agent_id: string | null;
    status: string;
    progress: number;
    plan_item_key: string | null;
    expected_output: string;
    output_type: string;
    blocked_reason: string;
    outputs: Array<{
      id: string;
      title: string;
      output_type: string;
      content: unknown;
      created_at: string;
    }>;
    approvals: Array<{
      id: string;
      task_id: string | null;
      conversation_id: string | null;
      agent_id: string | null;
      title: string;
      description: string;
      status: string;
      risk_level: string;
      type: string;
      payload: Record<string, unknown>;
      resolved_by: string;
      resolved_at: string | null;
      created_at: string;
    }>;
    runs: Array<{
      id: string;
      status: string;
      attempt_no: number;
      error: string;
      created_at: string;
      started_at: string | null;
      completed_at: string | null;
    }>;
    business_actions: BusinessAction[];
  }>;
  dependencies: Array<{ task_id: string; depends_on_task_id: string }>;
};

type TaskRunTrack = {
  id: string;
  task_id: string;
  status: string;
  attempt_no: number;
  error: string;
  lease_owner: string | null;
  lease_expires_at: string | null;
  started_at: string | null;
  created_at: string;
  completed_at: string | null;
  steps: RunStepTrace[];
  business_actions: BusinessAction[];
};

type BusinessAction = {
  id: string;
  run_id: string | null;
  task_id: string | null;
  conversation_id: string | null;
  agent_id: string | null;
  capability_key: string;
  tool_name: string;
  preview: {
    to?: string[];
    subject?: string;
    body_preview?: string;
    channel_id?: string;
  };
  status: string;
  approval_id: string | null;
  provider: string;
  external_id: string;
  error: string;
  attempt_no: number;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
};

type ContentPackageData = {
  platform: string;
  audience: string;
  objective: string;
  schedule: Array<{
    publish_at: string;
    order: number;
    content_type: string;
    title: string;
    hook: string;
    body?: string | null;
    script?: string | null;
    cta: string;
    asset_suggestion: string;
    source_refs: string[];
  }>;
  sources: Array<{ id?: string | null; title: string; url?: string | null }>;
  assumptions: string[];
};

type Approval = {
  id: string;
  taskId: string | null;
  conversationId: string | null;
  agentId: string | null;
  title: string;
  description: string;
  status: 'pending' | 'approved' | 'rejected' | string;
  riskLevel: string;
  category: string;
  payload: Record<string, unknown>;
  resolvedBy: string;
  resolvedAt: string | null;
  time: string;
};

type AgentExperience = {
  id: string;
  agentId: string;
  taskId: string | null;
  outcome: 'success' | 'lesson' | string;
  summary: string;
  lessons: string;
  time: string;
};

type AgentMemory = {
  id: string;
  memory_type: 'observation' | 'episode' | 'reflection' | 'relationship' | 'lesson' | string;
  title: string;
  content: string;
  importance: number;
  confidence: number;
  is_private: number;
  promoted: number;
  evidence_event_ids: string[];
  created_at: string;
};

type CompanyEventAudit = {
  id: string;
  title: string;
  actor_name: string;
  occurred_at: string;
};

type KnowledgeSource = {
  id: string;
  title: string;
  category: string;
  content: string;
  createdBy: string;
  createdAt: string;
  updatedAt: string;
  origin?: string;
  sourceRef?: string;
};

type ManagedObsidianDocument = {
  relative_path: string;
  title: string;
  content: string;
  modified_at: string;
};

type HireTemplate = {
  id: string;
  name: string;
  categoryId: string;
  category: string;
  dept: string;
  desc: string;
  prompt: string;
  skills: string[];
  mcps: string[];
  publisher: string;
  version: string;
  status: string;
};

type TalentCategory = {
  id: string;
  name: string;
  description: string;
  sortOrder: number;
};

type ToastState = {
  visible: boolean;
  message: string;
};

type ApiBootstrap = {
  workspace: Workspace;
  departments: Department[];
  agents: Array<{
    id: string;
    name: string;
    role: string;
    description: string;
    department_id: string;
    prompt: string;
    hue: number;
    glyph: string;
    status_kind: string;
    status_label: string;
    joined: string;
    skills: string[];
    mcps: string[];
  }>;
  conversations: Array<{
    id: string;
    kind: 'dm' | 'group';
    name: string;
    agent_id: string | null;
    member_ids: string[];
    unread: number;
    updated_at: string;
  }>;
  messages_by_conversation: Record<
    string,
    Array<{
      id: string;
      sender_type: 'user' | 'agent' | 'system';
      sender_id: string;
      content: string;
      created_at: string;
      provider?: string | null;
      model?: string | null;
    }>
  >;
  tasks: Array<{
    id: string;
    title: string;
    description: string;
    priority: string;
    owner_agent_id: string | null;
    suggested_agent_id?: string | null;
    suggested_agent_reason?: string;
    status: string;
    progress: number;
    conversation_id: string | null;
    due_date?: string | null;
    parent_task_id?: string | null;
    task_plan_id?: string | null;
    plan_item_key?: string | null;
    expected_output?: string;
    output_type?: string;
    created_at: string;
    updated_at: string;
  }>;
  knowledge_sources: Array<{
    id: string;
    title: string;
    category: string;
    content: string;
    created_by: string;
    created_at: string;
    updated_at: string;
    origin?: string;
    source_ref?: string;
  }>;
  task_events_by_task: Record<
    string,
    Array<{
      id: string;
      task_id: string;
      conversation_id: string | null;
      agent_id: string | null;
      kind: string;
      title: string;
      content: string;
      created_at: string;
    }>
  >;
  task_outputs_by_task: Record<
    string,
    Array<{
      id: string;
      task_id: string;
      conversation_id: string | null;
      agent_id: string | null;
      title: string;
      output_type: string;
      content: string;
      created_at: string;
    }>
  >;
  approvals_by_task: Record<
    string,
    Array<{
      id: string;
      task_id: string | null;
      conversation_id: string | null;
      agent_id: string | null;
      title: string;
      description: string;
      status: string;
      risk_level: string;
      type?: string;
      payload?: Record<string, unknown>;
      resolved_by: string;
      resolved_at: string | null;
      created_at: string;
    }>
  >;
  agent_experiences_by_agent: Record<
    string,
    Array<{
      id: string;
      agent_id: string;
      task_id: string | null;
      outcome: string;
      summary: string;
      lessons: string;
      created_at: string;
    }>
  >;
  agent_templates: Array<{
    id: string;
    name: string;
    category_id: string;
    category: string;
    department: string;
    description: string;
    prompt: string;
    skills: string[];
    mcps: string[];
    publisher?: string;
    version?: string;
    status?: string;
  }>;
  agent_template_categories: Array<{
    id: string;
    name: string;
    description: string;
    sort_order: number;
  }>;
  anomaly_count_24h?: number;
};

const themeOptions: Array<{
  mode: ThemeMode;
  icon: string;
  labelKey: 'nav.light' | 'nav.dark' | 'nav.system';
}> = [
  { mode: 'light', icon: 'light_mode', labelKey: 'nav.light' },
  { mode: 'dark', icon: 'dark_mode', labelKey: 'nav.dark' },
  { mode: 'system', icon: 'computer', labelKey: 'nav.system' },
];

const apiBaseUrl = import.meta.env.DEV
  ? (import.meta.env.VITE_AGENTPULSE_API_URL ?? 'http://127.0.0.1:8000/api')
  : 'https://api.agentpulse.cc/api';

let messageCounter = 1;
const tempMessageId = () => `temp_${messageCounter++}`;

const materialIcon = (name: string, className?: string) => (
  <span
    aria-hidden="true"
    className={className ? `material-symbol ${className}` : 'material-symbol'}
  >
    {name}
  </span>
);

const avatarColor = (agent: Agent) => `oklch(0.55 0.11 ${agent.hue})`;

function avatarText(name: string) {
  const normalized = name.trim();
  if (!normalized) return '?';
  return Array.from(normalized).slice(0, 2).join('');
}

function getInitialThemeMode(): ThemeMode {
  const stored = localStorage.getItem('agentpulse_theme_mode');
  return stored === 'light' || stored === 'dark' || stored === 'system'
    ? stored
    : 'light';
}

function getSystemTheme(): EffectiveTheme {
  return window.matchMedia('(prefers-color-scheme: dark)').matches
    ? 'dark'
    : 'light';
}

function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function getMentionContext(value: string, cursor: number) {
  if (cursor < 0) return null;
  const beforeCursor = value.slice(0, cursor);
  const match = /(^|\s)@([^\s@]*)$/.exec(beforeCursor);
  if (!match) return null;

  return {
    query: match[2],
    start: beforeCursor.length - match[2].length - 1,
  };
}

function renderMentionText(text: string, agents: Agent[]): ReactNode {
  const mentionNames = Array.from(
    new Set([...agents.map((agent) => agent.name), '老板']),
  )
    .filter(Boolean)
    .sort((left, right) => right.length - left.length);

  if (!mentionNames.length) return text;

  const pattern = new RegExp(
    `@(${mentionNames.map(escapeRegExp).join('|')})(?=\\s|$|[，。！？、,.!?;；:：])`,
    'g',
  );
  const nodes: ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(text.slice(lastIndex, match.index));
    }
    nodes.push(
      <span className="mention-token" key={`${match.index}-${match[0]}`}>
        {match[0]}
      </span>,
    );
    lastIndex = match.index + match[0].length;
  }

  if (lastIndex < text.length) nodes.push(text.slice(lastIndex));
  return nodes.length ? nodes : text;
}

function resolveMentionedAgent(
  text: string,
  memberIds: string[],
  agents: Agent[],
) {
  return agents.find(
    (agent) =>
      memberIds.includes(agent.id) &&
      new RegExp(
        `@${escapeRegExp(agent.name)}(?=\\s|$|[，。！？、,.!?;；:：])`,
      ).test(text),
  );
}

function dotColor(status: AgentStatus) {
  return {
    busy: '#3B5BDB',
    wait: '#D97706',
    stuck: '#DC2626',
    idle: '#9AA1AD',
  }[status];
}

function priorityStyle(priority: Priority) {
  if (priority === 'P0') return { background: '#FEECEA', color: '#B42318' };
  if (priority === 'P1') return { background: '#FEF3E2', color: '#B45309' };
  return { background: '#F2F4F7', color: '#475467' };
}

function statusStyle(status: TaskStatus) {
  if (status === '待执行')
    return { background: '#F0F7F5', color: '#18766A', bar: '#2A8C7F' };
  if (status === '待认领')
    return { background: '#F2F4F7', color: '#475467', bar: '#98A2B3' };
  if (status === '进行中')
    return { background: '#EEF1FB', color: '#3B5BDB', bar: '#3B5BDB' };
  if (status === '待确认')
    return { background: '#FEF3E2', color: '#B45309', bar: '#D97706' };
  if (status === '阻塞')
    return { background: '#FDF1F0', color: '#C0392B', bar: '#DC2626' };
  return { background: '#EEF4EE', color: '#16803C', bar: '#16A34A' };
}

function formatTime(value: string) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
  });
}

function normalizeStatus(value: string): AgentStatus {
  return value === 'busy' ||
    value === 'wait' ||
    value === 'stuck' ||
    value === 'idle'
    ? value
    : 'idle';
}

function normalizePriority(value: string): Priority {
  return value === 'P0' || value === 'P1' || value === 'P2' ? value : 'P2';
}

function normalizeTaskStatus(value: string): TaskStatus {
  return value === '进行中' ||
    value === '待执行' ||
    value === '待认领' ||
    value === '待确认' ||
    value === '阻塞' ||
    value === '已完成'
    ? value
    : value === '卡住'
      ? '阻塞'
      : '进行中';
}

function nextTaskStatus(status: TaskStatus): TaskStatus {
  if (status === '待执行') return '进行中';
  if (status === '待认领') return '进行中';
  if (status === '进行中') return '待确认';
  if (status === '待确认') return '已完成';
  if (status === '阻塞') return '进行中';
  return '已完成';
}

function progressForNextStatus(status: TaskStatus, current: number) {
  if (status === '已完成') return 100;
  if (status === '待认领') return 0;
  if (status === '待执行') return 0;
  if (status === '待确认') return Math.max(current, 80);
  if (status === '进行中') return Math.max(current, 20);
  return current;
}

function authHeaders(token: string) {
  return { Authorization: `Bearer ${token}` };
}

async function apiRequest<T>(
  path: string,
  options: RequestInit & { token?: string } = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set('Content-Type', 'application/json');
  if (options.token) headers.set('Authorization', `Bearer ${options.token}`);

  const response = await fetch(`${apiBaseUrl}${path}`, {
    ...options,
    headers,
  });
  const payload = await response
    .json()
    .catch(() => ({ detail: '后端返回了无法解析的响应' }));
  if (!response.ok) {
    throw new Error(formatApiError(payload, response.status));
  }
  return payload as T;
}

function formatApiError(payload: unknown, status: number) {
  if (payload && typeof payload === 'object' && 'detail' in payload) {
    const detail = (payload as { detail: unknown }).detail;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
      const messages = detail
        .map((item) => {
          if (!item || typeof item !== 'object') return '';
          const record = item as {
            loc?: unknown[];
            msg?: unknown;
            type?: unknown;
          };
          const field =
            Array.isArray(record.loc) && record.loc.length
              ? fieldLabel(String(record.loc.at(-1)))
              : '字段';
          const message =
            typeof record.msg === 'string' ? record.msg : '格式不正确';
          if (
            typeof record.type === 'string' &&
            record.type.includes('string_too_short')
          ) {
            return `${field}不能为空`;
          }
          return `${field}${message}`;
        })
        .filter(Boolean);
      if (messages.length) return messages.join('，');
    }
  }
  return `请求失败：${status}`;
}

function fieldLabel(field: string) {
  return (
    {
      email: '邮箱',
      password: '密码',
      display_name: '你的称呼',
      workspace_name: '公司/工作室名称',
      content: '消息',
      title: '标题',
      category: '分类',
    }[field] ?? field
  );
}

function excerpt(value: string, maxLength = 120) {
  const normalized = value.replace(/\s+/g, ' ').trim();
  return normalized.length > maxLength
    ? `${normalized.slice(0, maxLength - 1)}...`
    : normalized;
}

function mapBootstrap(data: ApiBootstrap) {
  const departmentsById = new Map(
    data.departments.map((department) => [department.id, department]),
  );
  const agents: Agent[] = data.agents.map((agent) => ({
    id: agent.id,
    name: agent.name,
    role: agent.role,
    description: agent.description,
    dept: departmentsById.get(agent.department_id)?.name ?? '未分配',
    departmentId: agent.department_id,
    hue: agent.hue,
    glyph: agent.glyph,
    statusKind: normalizeStatus(agent.status_kind),
    statusLabel: agent.status_label,
    joined: agent.joined,
    prompt: agent.prompt,
    skills: agent.skills,
    mcps: agent.mcps,
    experiences: (data.agent_experiences_by_agent[agent.id] ?? []).map(
      (experience) => ({
        id: experience.id,
        agentId: experience.agent_id,
        taskId: experience.task_id,
        outcome: experience.outcome,
        summary: experience.summary,
        lessons: experience.lessons,
        time: formatTime(experience.created_at),
      }),
    ),
  }));
  const agentById = new Map(agents.map((agent) => [agent.id, agent]));
  const chats: Chat[] = data.conversations.map((chat) => {
    if (chat.kind === 'dm') {
      return {
        id: chat.id,
        kind: 'dm',
        agentId: chat.agent_id ?? chat.member_ids[0] ?? '',
        unread: chat.unread,
        time: formatTime(chat.updated_at),
      };
    }
    return {
      id: chat.id,
      kind: 'group',
      name: chat.name,
      memberIds: chat.member_ids,
      unread: chat.unread,
      time: formatTime(chat.updated_at),
    };
  });
  const messagesByChat: Record<string, Message[]> = Object.fromEntries(
    Object.entries(data.messages_by_conversation).map(
      ([conversationId, rows]) => [
        conversationId,
        rows.map((message) => ({
          id: message.id,
          from:
            message.sender_type === 'user'
              ? 'boss'
              : message.sender_type === 'system'
                ? 'system'
                : message.sender_id,
          type: message.sender_type === 'system' ? 'system' : 'text',
          time: formatTime(message.created_at),
          text: message.content,
          provider: message.provider ?? undefined,
          model: message.model ?? undefined,
        })),
      ],
    ),
  );
  const chatById = new Map(chats.map((chat) => [chat.id, chat]));
  const tasks: Task[] = data.tasks.map((task) => {
    const chat = task.conversation_id
      ? chatById.get(task.conversation_id)
      : null;
    const owner = task.owner_agent_id
      ? agentById.get(task.owner_agent_id)
      : null;
    return {
      id: task.id,
      title: task.title,
      description: task.description,
      pr: normalizePriority(task.priority),
      owner: task.owner_agent_id ?? '',
      suggestedAgentId: task.suggested_agent_id ?? null,
      suggestedAgentReason: task.suggested_agent_reason ?? '',
      status: normalizeTaskStatus(task.status),
      progress: task.progress,
      src: task.conversation_id ?? '',
      srcLabel: chat
        ? chat.kind === 'dm'
          ? `私聊 · ${owner?.name ?? '员工'}`
          : `#${chat.name}`
        : '未关联会话',
      dueDate: task.due_date ?? null,
      parentTaskId: task.parent_task_id ?? null,
      taskPlanId: task.task_plan_id ?? null,
      planItemKey: task.plan_item_key ?? null,
      expectedOutput: task.expected_output ?? '',
      outputType: task.output_type ?? 'markdown',
      createdAt: formatTime(task.created_at),
      updatedAt: formatTime(task.updated_at),
      events: (data.task_events_by_task[task.id] ?? []).map((event) => ({
        id: event.id,
        taskId: event.task_id,
        conversationId: event.conversation_id,
        agentId: event.agent_id,
        kind: event.kind,
        title: event.title,
        content: event.content,
        time: formatTime(event.created_at),
      })),
      outputs: (data.task_outputs_by_task[task.id] ?? []).map((output) => ({
        id: output.id,
        taskId: output.task_id,
        conversationId: output.conversation_id,
        agentId: output.agent_id,
        title: output.title,
        outputType: output.output_type,
        content: output.content,
        time: formatTime(output.created_at),
      })),
      approvals: (data.approvals_by_task[task.id] ?? []).map((approval) => ({
        id: approval.id,
        taskId: approval.task_id,
        conversationId: approval.conversation_id,
        agentId: approval.agent_id,
        title: approval.title,
        description: approval.description,
        status: approval.status,
        riskLevel: approval.risk_level,
        category: approval.type ?? 'high_risk',
        payload: approval.payload ?? {},
        resolvedBy: approval.resolved_by,
        resolvedAt: approval.resolved_at
          ? formatTime(approval.resolved_at)
          : null,
        time: formatTime(approval.created_at),
      })),
    };
  });
  const templates: HireTemplate[] = data.agent_templates.map((template) => ({
    id: template.id,
    name: template.name,
    categoryId: template.category_id,
    category: template.category,
    dept: template.department,
    desc: template.description,
    prompt: template.prompt,
    skills: template.skills,
    mcps: template.mcps,
    publisher: template.publisher ?? 'AgentPulse 官方',
    version: template.version ?? 'v0.1.0',
    status: template.status ?? 'published',
  }));
  const talentCategories: TalentCategory[] = data.agent_template_categories
    .map((category) => ({
      id: category.id,
      name: category.name,
      description: category.description,
      sortOrder: category.sort_order,
    }))
    .sort((left, right) => left.sortOrder - right.sortOrder);
  const knowledgeSources: KnowledgeSource[] = data.knowledge_sources.map(
    (source) => ({
      id: source.id,
      title: source.title,
      category: source.category,
      content: source.content,
      createdBy: source.created_by,
      createdAt: formatTime(source.created_at),
      updatedAt: formatTime(source.updated_at),
      origin: source.origin,
      sourceRef: source.source_ref,
    }),
  );

  return {
    workspace: data.workspace,
    departments: data.departments,
    agents,
    chats,
    messagesByChat,
    tasks,
    knowledgeSources,
    templates,
    talentCategories,
  };
}

function App() {
  const { t } = useTranslation();
  const [themeMode, setThemeMode] = useState<ThemeMode>(getInitialThemeMode);
  const [systemTheme, setSystemTheme] =
    useState<EffectiveTheme>(getSystemTheme);
  const [token, setToken] = useState<string | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [sessionLoading, setSessionLoading] = useState(true);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [anomalyCount24h, setAnomalyCount24h] = useState(0);
  const [departments, setDepartments] = useState<Department[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [chats, setChats] = useState<Chat[]>([]);
  const [messagesByChat, setMessagesByChat] = useState<
    Record<string, Message[]>
  >({});
  const [tasks, setTasks] = useState<Task[]>([]);
  const [taskPlans, setTaskPlans] = useState<Record<string, TaskPlanSnapshot>>({});
  const [knowledgeSources, setKnowledgeSources] = useState<KnowledgeSource[]>(
    [],
  );
  const [hireTemplates, setHireTemplates] = useState<HireTemplate[]>([]);
  const [talentCategories, setTalentCategories] = useState<TalentCategory[]>(
    [],
  );
  const [bootLoading, setBootLoading] = useState(false);
  const [authError, setAuthError] = useState('');
  const [view, setView] = useState<View>('chat');
  const [chatId, setChatId] = useState('');
  const [draft, setDraft] = useState('');
  const [detailId, setDetailId] = useState<string | null>(null);
  const [hireOpen, setHireOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [teamCompilerOpen, setTeamCompilerOpen] = useState(false);
  const [teamDescription, setTeamDescription] = useState('');
  const [teamDrafts, setTeamDrafts] = useState<TeamMemberDraft[] | null>(null);
  const [teamDraftLoading, setTeamDraftLoading] = useState(false);
  const [teamDraftError, setTeamDraftError] = useState('');
  const [teamCreating, setTeamCreating] = useState(false);
  const [groupOpen, setGroupOpen] = useState(false);
  const [inviteOpen, setInviteOpen] = useState(false);
  const [taskOpen, setTaskOpen] = useState(false);
  const [knowledgeOpen, setKnowledgeOpen] = useState(false);
  const [taskDetailId, setTaskDetailId] = useState<string | null>(null);
  const [claimTaskId, setClaimTaskId] = useState<string | null>(null);
  const [claimAgentId, setClaimAgentId] = useState('');
  const [taskFilter, setTaskFilter] = useState<TaskStatus | '全部'>('全部');
  const [libraryTab, setLibraryTab] = useState<LibraryTab>('docs');
  const [typingName, setTypingName] = useState<string | null>(null);
  const [toast, setToast] = useState<ToastState>({
    visible: false,
    message: '',
  });
  const [hireTpl, setHireTpl] = useState('');
  const [hireDept, setHireDept] = useState('');
  const [createName, setCreateName] = useState('');
  const [createDesc, setCreateDesc] = useState('');
  const [createDept, setCreateDept] = useState('');
  const [createPrompt, setCreatePrompt] = useState('');
  const [createCapKeys, setCreateCapKeys] = useState<string[]>([]);
  const [groupName, setGroupName] = useState('');
  const [groupMembers, setGroupMembers] = useState<string[]>([]);
  const [groupTaskIds, setGroupTaskIds] = useState<string[]>([]);
  const [inviteMembers, setInviteMembers] = useState<string[]>([]);
  const [taskScopeChatId, setTaskScopeChatId] = useState<string | null>(null);
  const [taskTitle, setTaskTitle] = useState('');
  const [taskDesc, setTaskDesc] = useState('');
  const [taskPriority, setTaskPriority] = useState<Priority>('P2');
  const [taskOwnerId, setTaskOwnerId] = useState('');
  const [taskConversationId, setTaskConversationId] = useState('');
  const [knowledgeTitle, setKnowledgeTitle] = useState('');
  const [knowledgeCategory, setKnowledgeCategory] = useState('品牌资料');
  const [knowledgeContent, setKnowledgeContent] = useState('');
  const [obsidianSyncing, setObsidianSyncing] = useState(false);
  const [onboardingOpen, setOnboardingOpen] = useState(false);
  const [onboardingStep, setOnboardingStep] = useState(0);
  const [modelProvider, setModelProvider] =
    useState<ModelProviderStatus | null>(null);
  const [modelProviderLoading, setModelProviderLoading] = useState(false);
  const [modelProviderOpen, setModelProviderOpen] = useState(false);
  const [modelProviderSaving, setModelProviderSaving] = useState(false);
  const [modelProviderError, setModelProviderError] = useState('');
  const modelProviderPrompted = useRef(false);
  const messagesRef = useRef<HTMLDivElement>(null);
  const toastTimer = useRef<number | undefined>(undefined);

  const showToast = (message: string) => {
    window.clearTimeout(toastTimer.current);
    setToast({ visible: true, message });
    toastTimer.current = window.setTimeout(
      () => setToast({ visible: false, message: '' }),
      2200,
    );
  };

  const applyBootstrap = (data: ApiBootstrap) => {
    const mapped = mapBootstrap(data);
    setWorkspace(mapped.workspace);
    setAnomalyCount24h(data.anomaly_count_24h ?? 0);
    setDepartments(mapped.departments);
    setAgents(mapped.agents);
    setChats(mapped.chats);
    setMessagesByChat(mapped.messagesByChat);
    setTasks(mapped.tasks);
    setKnowledgeSources(mapped.knowledgeSources);
    setHireTemplates(mapped.templates);
    setTalentCategories(mapped.talentCategories);
    setOnboardingOpen(!mapped.workspace.onboarding_completed);

    const preferredChat =
      mapped.chats.find((chat) => chat.kind === 'dm' && chat.agentId) ??
      mapped.chats[0];
    if (preferredChat && !mapped.chats.some((chat) => chat.id === chatId)) {
      setChatId(preferredChat.id);
    }
  };

  const loadBootstrap = async (activeToken = token) => {
    if (!activeToken) return;
    setBootLoading(true);
    try {
      const data = await apiRequest<ApiBootstrap>('/me/bootstrap', {
        token: activeToken,
      });
      applyBootstrap(data);
      setAuthError('');
    } catch (error) {
      setAuthError(error instanceof Error ? error.message : '加载工作台失败');
      void window.agentpulse?.session.clear();
      setToken(null);
      setUser(null);
      setWorkspace(null);
    } finally {
      setBootLoading(false);
    }
  };

  const loadModelProvider = async (activeToken = token) => {
    if (!activeToken) return;
    setModelProviderLoading(true);
    try {
      const status = await apiRequest<ModelProviderStatus>(
        '/settings/model-provider',
        { token: activeToken },
      );
      setModelProvider(status);
      if (!status.configured && !modelProviderPrompted.current) {
        modelProviderPrompted.current = true;
        setModelProviderOpen(true);
      }
    } catch (error) {
      showToast(error instanceof Error ? error.message : '模型配置加载失败');
    } finally {
      setModelProviderLoading(false);
    }
  };

  // Brief API handlers (ADR 0006)
  const confirmBrief = async (briefId: string): Promise<boolean> => {
    if (!token) return false;
    try {
      const plan = await apiRequest<TaskPlanSnapshot>(`/briefs/${briefId}/launch`, {
        token,
        method: 'POST',
      });
      setTaskPlans((current) => ({ ...current, [plan.id]: plan }));
      showToast('计划已启动，员工会按依赖自动接力');
      await loadBootstrap();
      return true;
    } catch (error) {
      showToast(error instanceof Error ? error.message : '确认失败');
      return false;
    }
  };

  const rejectBrief = async (briefId: string): Promise<boolean> => {
    if (!token) return false;
    try {
      const rejected = await apiRequest<{
        id: string;
        status: string;
        goal: string;
      }>(`/briefs/${briefId}/reject`, {
        token,
        method: 'POST',
      });
      showToast(`已拒绝，继续讨论：${rejected.goal}`);
      await loadBootstrap();
      return true;
    } catch (error) {
      showToast(error instanceof Error ? error.message : '拒绝失败');
      return false;
    }
  };

  useEffect(() => {
    localStorage.setItem('agentpulse_theme_mode', themeMode);
  }, [themeMode]);

  useEffect(() => {
    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
    const updateSystemTheme = () =>
      setSystemTheme(mediaQuery.matches ? 'dark' : 'light');

    updateSystemTheme();
    mediaQuery.addEventListener('change', updateSystemTheme);
    return () => mediaQuery.removeEventListener('change', updateSystemTheme);
  }, []);

  useEffect(() => {
    let cancelled = false;
    const restoreSession = async () => {
      try {
        const stored = await window.agentpulse?.session.get();
        if (!cancelled && stored?.accessToken && stored.user) {
          setToken(stored.accessToken);
          setUser(stored.user);
        }
      } finally {
        if (!cancelled) setSessionLoading(false);
      }
    };
    void restoreSession();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!sessionLoading && token && user) {
      void Promise.all([loadBootstrap(token), loadModelProvider(token)]);
    }
  }, [sessionLoading]);

  const activePlanIds = useMemo(
    () =>
      Array.from(
        new Set(
          tasks
            .filter((task) => task.taskPlanId && task.status !== '已完成')
            .map((task) => task.taskPlanId as string),
        ),
      ),
    [tasks],
  );

  useEffect(() => {
    if (!token || activePlanIds.length === 0) return;
    let cancelled = false;
    const refresh = async () => {
      try {
        const plans = await Promise.all(
          activePlanIds.map((planId) =>
            apiRequest<TaskPlanSnapshot>(`/task-plans/${planId}`, { token }),
          ),
        );
        if (cancelled) return;
        setTaskPlans((current) => ({
          ...current,
          ...Object.fromEntries(plans.map((plan) => [plan.id, plan])),
        }));
        const taskById = new Map(
          plans.flatMap((plan) => plan.tasks).map((task) => [task.id, task]),
        );
        setTasks((current) =>
          current.map((task) => {
            const snapshot = taskById.get(task.id);
            return snapshot
              ? {
                  ...task,
                  status: normalizeTaskStatus(snapshot.status),
                  progress: snapshot.progress,
                  outputs: snapshot.outputs.map((output) => ({
                    id: output.id,
                    taskId: snapshot.id,
                    conversationId: task.src || null,
                    agentId: snapshot.owner_agent_id,
                    title: output.title,
                    outputType: output.output_type,
                    content:
                      typeof output.content === 'string'
                        ? output.content
                        : JSON.stringify(output.content),
                    time: formatTime(output.created_at),
                  })),
                  approvals: snapshot.approvals.map((approval) => ({
                    id: approval.id,
                    taskId: approval.task_id,
                    conversationId: approval.conversation_id,
                    agentId: approval.agent_id,
                    title: approval.title,
                    description: approval.description,
                    status: approval.status,
                    riskLevel: approval.risk_level,
                    category: approval.type ?? 'high_risk',
                    payload: approval.payload ?? {},
                    resolvedBy: approval.resolved_by,
                    resolvedAt: approval.resolved_at
                      ? formatTime(approval.resolved_at)
                      : null,
                    time: formatTime(approval.created_at),
                  })),
                }
              : task;
          }),
        );
      } catch {
        // A transient poll failure should not interrupt the active workspace.
      }
    };
    void refresh();
    const timer = window.setInterval(refresh, 3000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [token, activePlanIds.join('|')]);

  useEffect(() => {
    if (!token || !chatId) return;
    let cancelled = false;
    const refreshApprovals = async () => {
      try {
        const pending = await apiRequest<
          Array<{
            id: string;
            type: string;
            title: string;
            description: string;
            payload: Record<string, unknown>;
          }>
        >(`/conversations/${chatId}/approvals?status=pending`, { token });
        if (cancelled || pending.length === 0) return;
        setMessagesByChat((current) => {
          const messages = current[chatId] ?? [];
          const additions = pending
            .filter(
              (approval) =>
                !messages.some(
                  (message) =>
                    message.id === `approval_${approval.id}` ||
                    (message.text.startsWith('APPROVAL_CARD:') &&
                      message.text.includes(`"approval_id":"${approval.id}"`)),
                ),
            )
            .map((approval): Message => ({
              id: `approval_${approval.id}`,
              from: 'system',
              type: 'system',
              time: '',
              text:
                'APPROVAL_CARD:' +
                JSON.stringify({
                  approval_id: approval.id,
                  category: approval.type,
                  title: approval.title,
                  description: approval.description,
                  ...approval.payload,
                }),
            }));
          return additions.length
            ? { ...current, [chatId]: [...messages, ...additions] }
            : current;
        });
      } catch {
        // Pending approvals are restored on the next successful poll.
      }
    };
    void refreshApprovals();
    const timer = window.setInterval(refreshApprovals, 1000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [chatId, token]);

  useEffect(() => {
    messagesRef.current?.scrollTo({ top: messagesRef.current.scrollHeight });
  }, [chatId, messagesByChat, typingName]);

  useEffect(() => {
    return () => window.clearTimeout(toastTimer.current);
  }, []);

  const effectiveTheme = themeMode === 'system' ? systemTheme : themeMode;

  const handleAuthSuccess = async (payload: {
    access_token: string;
    user: User;
    workspace: Workspace;
  }) => {
    await window.agentpulse?.session.set({
      accessToken: payload.access_token,
      user: payload.user,
    });
    setToken(payload.access_token);
    setUser(payload.user);
    setWorkspace(payload.workspace);
    modelProviderPrompted.current = false;
    await Promise.all([
      loadBootstrap(payload.access_token),
      loadModelProvider(payload.access_token),
    ]);
  };

  const logout = () => {
    void window.agentpulse?.session.clear();
    setToken(null);
    setUser(null);
    setWorkspace(null);
    setAgents([]);
    setChats([]);
    setMessagesByChat({});
    setTasks([]);
    setKnowledgeSources([]);
    setHireTemplates([]);
    setTalentCategories([]);
    setModelProvider(null);
    setModelProviderOpen(false);
    modelProviderPrompted.current = false;
  };

  const saveModelProvider = async (apiKey: string) => {
    if (!token) return;
    setModelProviderSaving(true);
    setModelProviderError('');
    try {
      const status = await apiRequest<ModelProviderStatus>(
        '/settings/model-provider',
        {
          token,
          method: 'PUT',
          body: JSON.stringify({ provider: 'deepseek', api_key: apiKey }),
        },
      );
      setModelProvider(status);
      setModelProviderOpen(false);
      showToast('DeepSeek 已连接，AI 团队正在就位');
      await loadBootstrap(token);
    } catch (error) {
      setModelProviderError(
        error instanceof Error ? error.message : 'DeepSeek Key 验证失败',
      );
    } finally {
      setModelProviderSaving(false);
    }
  };

  const revokeModelProvider = async () => {
    if (!token) return;
    const status = await apiRequest<ModelProviderStatus>(
      '/settings/model-provider',
      { token, method: 'DELETE' },
    );
    setModelProvider(status);
    showToast('DeepSeek Key 已撤销，新 Run 已停止');
    await loadBootstrap(token);
  };

  const agentById = (id: string) => agents.find((agent) => agent.id === id);
  const activeChat = chats.find((chat) => chat.id === chatId) ?? chats[0];
  const busyCount = agents.filter(
    (agent) => agent.statusKind === 'busy',
  ).length;
  const confirmTasks = tasks.filter((task) => task.status === '待确认');
  const stuckCount = tasks.filter((task) => task.status === '阻塞').length;
  const unreadTotal = chats.reduce((count, chat) => count + chat.unread, 0);
  const allSkills = Array.from(
    new Set(hireTemplates.flatMap((template) => template.skills)),
  );
  const allMcps = Array.from(
    new Set(hireTemplates.flatMap((template) => template.mcps)),
  );

  const lastMessagePreview = (id: string) => {
    const messages = messagesByChat[id] ?? [];
    const last = messages.at(-1);
    if (!last) return '开始对话';

    const author =
      last.from === 'boss'
        ? '我'
        : last.from === 'system'
          ? ''
          : (agentById(last.from)?.name ?? '');
    const prefix = author ? `${author}：` : '';

    return `${prefix}${last.text}`;
  };

  const openChat = (id: string) => {
    setView('chat');
    setChatId(id);
    setDraft('');
    setDetailId(null);
    setTaskDetailId(null);
    setChats((current) =>
      current.map((chat) => (chat.id === id ? { ...chat, unread: 0 } : chat)),
    );
  };

  const openHire = (templateId: string) => {
    const template = hireTemplates.find((item) => item.id === templateId);
    if (!template) return;
    setHireTpl(template.id);
    setHireDept(template.dept);
    setHireOpen(true);
  };

  const openCreateAgent = () => {
    setCreateName('');
    setCreateDesc('');
    setCreateDept('');
    setCreatePrompt('');
    setCreateCapKeys([]);
    setCreateOpen(true);
  };

  const openGroupModal = () => {
    setGroupName('');
    setGroupMembers([]);
    setGroupTaskIds([]);
    setGroupOpen(true);
  };

  const openAllTasks = () => {
    setTaskScopeChatId(null);
    setView('tasks');
    setDetailId(null);
    setTaskDetailId(null);
  };

  const openRelatedTasks = () => {
    if (!activeChat) return;
    setTaskScopeChatId(activeChat.id);
    setTaskFilter('全部');
    setView('tasks');
    setDetailId(null);
    setTaskDetailId(null);
  };

  const openCreateTask = () => {
    setTaskTitle('');
    setTaskDesc('');
    setTaskPriority('P2');
    setTaskOwnerId('');
    setTaskConversationId(taskScopeChatId ?? activeChat?.id ?? '');
    setTaskOpen(true);
  };

  const send = async () => {
    const text = draft.trim();
    if (!text || !activeChat || !token) return;

    const targetChat = activeChat;
    const memberIds =
      targetChat.kind === 'dm' ? [targetChat.agentId] : targetChat.memberIds;
    const mentionedAgent =
      targetChat.kind === 'group'
        ? resolveMentionedAgent(text, memberIds, agents)
        : null;
    const replierId =
      targetChat.kind === 'dm'
        ? targetChat.agentId
        : (mentionedAgent?.id ?? targetChat.memberIds[0]);
    const replier = replierId ? agentById(replierId) : null;
    if (!replier) return;
    const targetAgentId =
      targetChat.kind === 'group' ? (mentionedAgent?.id ?? null) : null;

    const optimisticId = tempMessageId();
    const optimisticMessage: Message = {
      id: optimisticId,
      from: 'boss',
      type: 'text',
      time: '刚刚',
      text,
    };
    setMessagesByChat((current) => ({
      ...current,
      [targetChat.id]: [...(current[targetChat.id] ?? []), optimisticMessage],
    }));
    setDraft('');
    setTypingName(
      targetChat.kind === 'group' && !mentionedAgent ? '群聊成员' : replier.name,
    );
    setAgents((current) =>
      current.map((agent) =>
        agent.id === replier.id
          ? { ...agent, statusKind: 'busy', statusLabel: '思考中' }
          : agent,
      ),
    );

    try {
      // Use SSE streaming endpoint
      const headers = new Headers();
      headers.set('Content-Type', 'application/json');
      headers.set('Authorization', `Bearer ${token}`);
      const sseResponse = await fetch(
        `${apiBaseUrl}/conversations/${targetChat.id}/messages/stream`,
        {
          method: 'POST',
          headers,
          body: JSON.stringify({ content: text, target_agent_id: targetAgentId }),
        },
      );
      if (!sseResponse.ok) {
        const errData = await sseResponse.json().catch(() => ({ detail: '请求失败' }));
        throw new Error(errData.detail || `HTTP ${sseResponse.status}`);
      }
      if (!sseResponse.body) throw new Error('No response body');

      const reader = sseResponse.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let currentAgentId = '';
      let currentAgentName = '';
      let streamingMessageId = '';
      let streamingContent = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        let currentEvent = '';
        for (const line of lines) {
          if (line.startsWith('event: ')) {
            currentEvent = line.slice(7).trim();
          } else if (line.startsWith('data: ') && currentEvent) {
            const dataStr = line.slice(6);
            try {
              const data = JSON.parse(dataStr);

              if (currentEvent === 'user_message') {
                // Replace optimistic message with real one
                const userMessage = mapApiMessage(data);
                setMessagesByChat((current) => ({
                  ...current,
                  [targetChat.id]: [
                    ...(current[targetChat.id] ?? []).filter(
                      (message) => message.id !== optimisticId,
                    ),
                    userMessage,
                  ],
                }));
              } else if (currentEvent === 'speaking') {
                // Show who is currently speaking
                currentAgentId = data.agent_id;
                currentAgentName = data.agent_name;
                streamingMessageId = `stream-${data.agent_id}-${Date.now()}`;
                streamingContent = '';
                setTypingName(data.agent_name);
                setAgents((current) =>
                  current.map((agent) =>
                    agent.id === data.agent_id
                      ? { ...agent, statusKind: 'busy', statusLabel: '发言中' }
                      : agent,
                  ),
                );
              } else if (currentEvent === 'chunk') {
                // Append chunk to streaming message
                streamingContent += data.content;
                const chunkContent = streamingContent;
                const chunkMsgId = streamingMessageId;
                const chunkAgentId = currentAgentId;
                setMessagesByChat((current) => {
                  const existing = current[targetChat.id] ?? [];
                  const hasStreamMsg = existing.some((m) => m.id === chunkMsgId);
                  const streamMsg: Message = {
                    id: chunkMsgId,
                    from: chunkAgentId as any,
                    type: 'text',
                    time: '刚刚',
                    text: chunkContent,
                  };
                  return {
                    ...current,
                    [targetChat.id]: hasStreamMsg
                      ? existing.map((m) => (m.id === chunkMsgId ? streamMsg : m))
                      : [...existing, streamMsg],
                  };
                });
              } else if (currentEvent === 'done') {
                // Replace streaming message with persisted one
                const finalMessage = mapApiMessage(data);
                const streamId = streamingMessageId;
                setMessagesByChat((current) => ({
                  ...current,
                  [targetChat.id]: (current[targetChat.id] ?? []).map((m) =>
                    m.id === streamId ? finalMessage : m,
                  ),
                }));
                streamingMessageId = '';
                streamingContent = '';
              } else if (currentEvent === 'approval') {
                // TD-06-T3: a run suspended for owner approval / clarification /
                // capability upgrade — drop an interactive card into the thread.
                const category = data.category || 'high_risk';
                const tool = data.tool_call || {};
                const title =
                  category === 'clarification'
                    ? '员工请求澄清'
                    : category === 'capability_upgrade'
                      ? '员工申请能力升级'
                      : `高风险动作需确认：${tool.title || tool.name || '高风险动作'}`;
                const description =
                  category === 'clarification'
                    ? tool.question || tool.text || ''
                    : category === 'capability_upgrade'
                      ? tool.capability_description || tool.text || ''
                      : tool.text || tool.title || '';
                const cardText =
                  'APPROVAL_CARD:' +
                  JSON.stringify({
                    approval_id: data.approval_id,
                    category,
                    title,
                    description,
                    suggested_capability_key: tool.suggested_capability_key,
                  });
                setMessagesByChat((current) => ({
                  ...current,
                  [targetChat.id]: [
                    ...(current[targetChat.id] ?? []),
                    {
                      id: tempMessageId(),
                      from: 'system' as const,
                      type: 'system' as const,
                      time: '',
                      text: cardText,
                    },
                  ],
                }));
              } else if (currentEvent === 'system') {
                // 讨论收敛后服务端落库的 BRIEF_CARD 系统消息——实时上屏，
                // 不用等流结束后的 bootstrap 刷新才看到共识卡片
                const systemMessage = mapApiMessage(data);
                setMessagesByChat((current) => ({
                  ...current,
                  [targetChat.id]: [...(current[targetChat.id] ?? []), systemMessage],
                }));
              } else if (currentEvent === 'error') {
                setMessagesByChat((current) => ({
                  ...current,
                  [targetChat.id]: [
                    ...(current[targetChat.id] ?? []),
                    {
                      id: tempMessageId(),
                      from: 'system' as const,
                      type: 'system' as const,
                      time: '',
                      text: `调用失败：${data.detail}`,
                    },
                  ],
                }));
              }
            } catch {
              // skip malformed data
            }
            currentEvent = '';
          }
        }
      }
      setChats((current) =>
        current.map((chat) =>
          chat.id === targetChat.id ? { ...chat, time: '刚刚' } : chat,
        ),
      );
      await loadBootstrap(token);
    } catch (error) {
      const errorMessage =
        error instanceof Error ? error.message : 'LLM 调用失败，请稍后重试';
      setMessagesByChat((current) => ({
        ...current,
        [targetChat.id]: [
          ...(current[targetChat.id] ?? []),
          {
            id: tempMessageId(),
            from: 'system',
            type: 'system',
            time: '',
            text: `真实调用 DeepSeek 失败：${errorMessage}`,
          },
        ],
      }));
      showToast('DeepSeek 调用失败，请检查后端和 API Key');
    } finally {
      setTypingName(null);
      setAgents((current) =>
        current.map((agent) =>
          agent.id === replier.id
            ? { ...agent, statusKind: 'idle', statusLabel: '在线待命' }
            : agent,
        ),
      );
    }
  };

  const submitHire = async () => {
    if (!token || !hireTpl) return;
    try {
      await apiRequest('/agents/recruit', {
        method: 'POST',
        token,
        body: JSON.stringify({
          template_id: hireTpl,
          department_name: hireDept,
        }),
      });
      await loadBootstrap(token);
      setHireOpen(false);
      setView('staff');
      showToast('招募成功，已加入组织架构');
    } catch (error) {
      showToast(error instanceof Error ? error.message : '招募失败');
    }
  };

  const submitCreateAgent = async () => {
    if (!token) return;
    const name = createName.trim();
    const prompt = createPrompt.trim();
    const departmentName = createDept.trim();
    if (!name || !prompt || !departmentName) {
      showToast('请填写员工名称、部门和工作职责 Prompt');
      return;
    }
    try {
      const body: Record<string, unknown> = {
        name,
        description: createDesc.trim(),
        department_name: departmentName,
        prompt,
      };
      if (createCapKeys.length > 0) {
        body.role_spec = {
          role_name: name,
          source_request: createDesc.trim() || name,
          capability_keys: createCapKeys,
        };
      }
      await apiRequest('/agents', {
        method: 'POST',
        token,
        body: JSON.stringify(body),
      });
      await loadBootstrap(token);
      setCreateOpen(false);
      setView('staff');
      showToast(`已创建「${name}」`);
    } catch (error) {
      showToast(error instanceof Error ? error.message : '创建失败');
    }
  };

  const openTeamCompiler = () => {
    setTeamDescription('');
    setTeamDrafts(null);
    setTeamDraftError('');
    setTeamCompilerOpen(true);
  };

  const draftTeamFromDescription = async () => {
    if (!token) return;
    const description = teamDescription.trim();
    if (!description) {
      showToast('先描述一下你想要的团队');
      return;
    }
    setTeamDraftLoading(true);
    setTeamDraftError('');
    try {
      const result = await apiRequest<{ members: TeamMemberDraft[] }>(
        '/agents/draft-team',
        { method: 'POST', token, body: JSON.stringify({ description }) },
      );
      setTeamDrafts(result.members);
    } catch (error) {
      setTeamDraftError(error instanceof Error ? error.message : '生成失败');
    } finally {
      setTeamDraftLoading(false);
    }
  };

  const updateTeamDraftMember = (index: number, patch: Partial<TeamMemberDraft>) => {
    setTeamDrafts((prev) =>
      prev ? prev.map((m, i) => (i === index ? { ...m, ...patch } : m)) : prev,
    );
  };

  const removeTeamDraftMember = (index: number) => {
    setTeamDrafts((prev) => (prev ? prev.filter((_, i) => i !== index) : prev));
  };

  const confirmCreateTeam = async () => {
    if (!token || !teamDrafts || teamDrafts.length === 0) return;
    setTeamCreating(true);
    try {
      const result = await apiRequest<{
        agents: { id: string; name: string }[];
        conversation_id: string | null;
      }>('/agents/create-team', {
        method: 'POST',
        token,
        body: JSON.stringify({ members: teamDrafts }),
      });
      await loadBootstrap(token);
      setTeamCompilerOpen(false);
      showToast(`已创建 ${result.agents.length} 位员工`);
      if (result.conversation_id) {
        setChatId(result.conversation_id);
        setView('chat');
      } else {
        setView('staff');
      }
    } catch (error) {
      showToast(error instanceof Error ? error.message : '创建团队失败');
    } finally {
      setTeamCreating(false);
    }
  };

  const createGroup = async () => {
    if (!token) return;
    if (!groupMembers.length) {
      showToast('至少拉一位员工进群');
      return;
    }
    try {
      const response = await apiRequest<{ id: string }>(
        '/conversations/group',
        {
          method: 'POST',
          token,
          body: JSON.stringify({
            name: groupName.trim() || '新的讨论',
            member_ids: groupMembers,
            related_task_ids: groupTaskIds,
          }),
        },
      );
      await loadBootstrap(token);
      setGroupOpen(false);
      setGroupName('');
      setGroupMembers([]);
      setGroupTaskIds([]);
      setView('chat');
      setChatId(response.id);
      showToast('群聊已创建，把事情说给他们听吧');
    } catch (error) {
      showToast(error instanceof Error ? error.message : '建群失败');
    }
  };

  const openInviteMembers = () => {
    if (!activeChat || activeChat.kind !== 'group') return;
    setInviteMembers([]);
    setInviteOpen(true);
  };

  const inviteGroupMembers = async () => {
    if (!token || !activeChat || activeChat.kind !== 'group') return;
    if (!inviteMembers.length) {
      showToast('请选择要拉入群聊的员工');
      return;
    }
    try {
      await apiRequest(`/conversations/${activeChat.id}/members`, {
        method: 'POST',
        token,
        body: JSON.stringify({
          member_ids: inviteMembers,
        }),
      });
      await loadBootstrap(token);
      setInviteOpen(false);
      setInviteMembers([]);
      showToast('已拉入群聊');
    } catch (error) {
      showToast(error instanceof Error ? error.message : '拉人失败');
    }
  };

  const submitCreateTask = async () => {
    if (!token) return;
    const title = taskTitle.trim();
    if (!title) {
      showToast('请填写任务标题');
      return;
    }
    try {
      await apiRequest('/tasks', {
        method: 'POST',
        token,
        body: JSON.stringify({
          title,
          description: taskDesc.trim(),
          priority: taskPriority,
          owner_agent_id: taskOwnerId || null,
          conversation_id: taskConversationId || null,
          status: taskOwnerId ? '进行中' : '待认领',
          progress: taskOwnerId ? 10 : 0,
        }),
      });
      await loadBootstrap(token);
      setTaskOpen(false);
      setView('tasks');
      showToast('任务已创建');
    } catch (error) {
      showToast(error instanceof Error ? error.message : '创建任务失败');
    }
  };

  const advanceTask = async (task: Task) => {
    if (!token) return;
    if (task.status === '待认领' && !task.owner) {
      showToast('请先让员工认领这个任务');
      return;
    }
    const nextStatus = nextTaskStatus(task.status);
    try {
      await apiRequest(`/tasks/${task.id}`, {
        method: 'PATCH',
        token,
        body: JSON.stringify({
          status: nextStatus,
          progress: progressForNextStatus(nextStatus, task.progress),
        }),
      });
      await loadBootstrap(token);
      showToast(`任务已更新为「${nextStatus}」`);
    } catch (error) {
      showToast(error instanceof Error ? error.message : '更新任务失败');
    }
  };

  const claimTask = async () => {
    if (!token || !claimTaskId || !claimAgentId) {
      showToast('请选择认领员工');
      return;
    }
    try {
      await apiRequest(`/tasks/${claimTaskId}/claim`, {
        method: 'POST',
        token,
        body: JSON.stringify({ agent_id: claimAgentId }),
      });
      await loadBootstrap(token);
      setClaimTaskId(null);
      setClaimAgentId('');
      showToast('任务已进入执行中');
    } catch (error) {
      showToast(error instanceof Error ? error.message : '认领任务失败');
    }
  };

  const submitKnowledgeSource = async () => {
    if (!token) return;
    const title = knowledgeTitle.trim();
    const content = knowledgeContent.trim();
    if (!title || !content) {
      showToast('请填写资料标题和正文');
      return;
    }
    try {
      await apiRequest('/knowledge-sources', {
        method: 'POST',
        token,
        body: JSON.stringify({
          title,
          category: knowledgeCategory.trim() || '通用资料',
          content,
        }),
      });
      await loadBootstrap(token);
      setKnowledgeOpen(false);
      setKnowledgeTitle('');
      setKnowledgeCategory('品牌资料');
      setKnowledgeContent('');
      setLibraryTab('docs');
      showToast('资料已写入公司资料库');
    } catch (error) {
      showToast(error instanceof Error ? error.message : '保存资料失败');
    }
  };

  const syncObsidian = async () => {
    if (!token || !window.agentpulse?.obsidian) {
      showToast('当前运行环境不支持 Obsidian 同步');
      return;
    }
    setObsidianSyncing(true);
    try {
      const selection = await window.agentpulse.obsidian.pickManaged();
      if (!selection) return;
      if (!selection.documents.length) {
        showToast('未找到 .agentpulse/managed 下的 Markdown 资料');
        return;
      }
      const result = await apiRequest<{ created: number; updated: number; unchanged: number }>(
        '/knowledge-sources/obsidian-sync',
        {
          method: 'POST',
          token,
          body: JSON.stringify({ documents: selection.documents }),
        },
      );
      await loadBootstrap(token);
      showToast(
        `Obsidian 已同步：新增 ${result.created}，更新 ${result.updated}，未变 ${result.unchanged}`,
      );
    } catch (error) {
      showToast(error instanceof Error ? error.message : 'Obsidian 同步失败');
    } finally {
      setObsidianSyncing(false);
    }
  };

  const resolveApproval = async (
    approval: Approval,
    status: 'approved' | 'rejected',
  ) => {
    if (!token) return;
    try {
      await apiRequest(`/approvals/${approval.id}/resolve`, {
        method: 'POST',
        token,
        body: JSON.stringify({ status }),
      });
      await loadBootstrap(token);
      showToast(
        status === 'approved' ? '已确认，任务归档完成' : '已驳回，任务进入阻塞',
      );
    } catch (error) {
      showToast(error instanceof Error ? error.message : '处理确认请求失败');
    }
  };

  // TD-06-T3 (chat cards): resolve a run-linked approval / capability upgrade
  // straight from the chat thread, then wake the suspended run.
  const resolveChatApproval = async (
    approvalId: string,
    status: 'approved' | 'rejected',
    approvedCapabilityKey?: string,
    scope: 'once' | 'always' = 'once',
  ): Promise<boolean> => {
    if (!token) return false;
    try {
      await apiRequest(`/approvals/${approvalId}/resolve`, {
        method: 'POST',
        token,
        body: JSON.stringify({
          status,
          approved_capability_key: approvedCapabilityKey ?? null,
          scope,
        }),
      });
      showToast(
        status === 'rejected'
          ? '已驳回'
          : scope === 'always'
            ? '已永久允许（下次同类不再询问）'
            : '已批准，员工继续执行',
      );
      return true;
    } catch (error) {
      showToast(error instanceof Error ? error.message : '处理失败');
      return false;
    }
  };

  const answerChatClarification = async (
    approvalId: string,
    answer: string,
  ): Promise<boolean> => {
    if (!token || !answer.trim()) return false;
    try {
      await apiRequest(`/approvals/${approvalId}/answer`, {
        method: 'POST',
        token,
        body: JSON.stringify({ answer: answer.trim() }),
      });
      showToast('已回复，员工继续');
      return true;
    } catch (error) {
      showToast(error instanceof Error ? error.message : '回复失败');
      return false;
    }
  };

  const finishOnboarding = async () => {
    if (token) {
      await apiRequest('/me/onboarding/complete', {
        method: 'POST',
        token,
        body: JSON.stringify({}),
      }).catch(() => null);
    }
    setOnboardingOpen(false);
    setWorkspace((current) =>
      current ? { ...current, onboarding_completed: true } : current,
    );
    showToast('团队已就位，从给小秘发条消息开始吧');
  };

  const scopedTasks = taskScopeChatId
    ? tasks.filter((task) => task.src === taskScopeChatId)
    : tasks;
  const filteredTasks = scopedTasks.filter(
    (task) => taskFilter === '全部' || task.status === taskFilter,
  );
  const taskTabs = (
    ['全部', '待执行', '待认领', '进行中', '待确认', '阻塞', '已完成'] as const
  ).map((status) => ({
    status,
    count:
      status === '全部'
        ? scopedTasks.length
        : scopedTasks.filter((task) => task.status === status).length,
  }));
  const libraryTabs: Array<{ key: LibraryTab; label: string }> = [
    { key: 'docs', label: t('library.tabDocs') },
    { key: 'skills', label: t('library.tabSkills') },
    { key: 'mcp', label: t('library.tabMcp') },
  ];

  const currentChatAgent =
    activeChat?.kind === 'dm' ? agentById(activeChat.agentId) : null;
  const chatTitle =
    activeChat?.kind === 'dm'
      ? currentChatAgent?.id
        ? `${currentChatAgent.name} · ${currentChatAgent.role}`
        : '消息'
      : activeChat
        ? `# ${activeChat.name}`
        : '消息';
  const relatedTasks = activeChat
    ? tasks.filter((task) => task.src === activeChat.id)
    : [];
  const activeChatApprovals = activeChat
    ? tasks
        .flatMap((task) =>
          task.approvals.map((approval) => ({ approval, task })),
        )
        .filter(
          ({ approval }) =>
            approval.status === 'pending' &&
            approval.conversationId === activeChat.id,
        )
    : [];
  const scopedChat = taskScopeChatId
    ? chats.find((chat) => chat.id === taskScopeChatId)
    : null;
  const taskScopeLabel =
    scopedChat?.kind === 'group'
      ? `# ${scopedChat.name}`
      : scopedChat?.kind === 'dm'
        ? (agentById(scopedChat.agentId)?.name ?? '私聊')
        : null;
  const chatMeta =
    activeChat?.kind === 'dm'
      ? `${currentChatAgent?.role ?? ''} · ${currentChatAgent?.statusLabel ?? ''}`
      : activeChat
        ? `${activeChat.memberIds.length} 名成员 · 讨论与执行结果都沉淀在这里`
        : '登录后开始工作';
  const chatMembers =
    activeChat?.kind === 'group'
      ? activeChat.memberIds
      : activeChat?.kind === 'dm'
        ? [activeChat.agentId]
        : [];
  const detailAgent = detailId ? agentById(detailId) : null;
  const detailTask = taskDetailId
    ? tasks.find((task) => task.id === taskDetailId)
    : null;

  if (sessionLoading) {
    return (
      <main className="workbench-shell auth-shell" data-theme={effectiveTheme}>
        <div className="loading-panel">正在恢复登录状态...</div>
      </main>
    );
  }

  if (!token || !user || !workspace) {
    return (
      <AuthScreen
        theme={effectiveTheme}
        error={authError}
        onAuthSuccess={handleAuthSuccess}
      />
    );
  }

  if (bootLoading) {
    return (
      <main className="workbench-shell auth-shell" data-theme={effectiveTheme}>
        <div className="loading-panel">正在加载你的 AI 公司...</div>
      </main>
    );
  }

  return (
    <main
      className="workbench-shell"
      data-theme={effectiveTheme}
      data-theme-mode={themeMode}
    >
      <Sidebar
        view={view}
        unreadTotal={unreadTotal}
        taskAlerts={confirmTasks.length + stuckCount}
        anomalyCount24h={anomalyCount24h}
        themeMode={themeMode}
        onThemeModeChange={setThemeMode}
        onLogout={logout}
        onNavigate={(nextView) => {
          if (nextView === 'tasks') setTaskScopeChatId(null);
          setView(nextView);
          setDetailId(null);
          setTaskDetailId(null);
        }}
      />

      {view === 'chat' && (
        <ConversationList
          chats={chats}
          activeChatId={chatId}
          agents={agents}
          lastMessagePreview={lastMessagePreview}
          onOpenChat={openChat}
          onOpenGroupModal={openGroupModal}
        />
      )}

      <section className="main-stage">
        {view === 'chat' && activeChat && (
          <ChatView
            conversationId={activeChat.id}
            token={token}
            title={chatTitle}
            meta={chatMeta}
            members={chatMembers}
            messages={messagesByChat[activeChat.id] ?? []}
            agents={agents}
            approvals={activeChatApprovals}
            relatedTasks={relatedTasks}
            relatedTaskCount={relatedTasks.length}
            draft={draft}
            placeholder={
              activeChat.kind === 'group'
                ? t('chat.sendPlaceholderGroup', { name: activeChat.name })
                : t('chat.sendPlaceholderDm', { name: currentChatAgent?.name ?? '' })
            }
            typingName={typingName}
            messagesRef={messagesRef}
            onDraftChange={setDraft}
            onSend={send}
            onOpenTasks={openRelatedTasks}
            onResolveApproval={resolveApproval}
            onOpenTask={(taskId) => setTaskDetailId(taskId)}
            onInviteMembers={
              activeChat.kind === 'group' ? openInviteMembers : undefined
            }
            onOpenAgent={(id) => setDetailId(id)}
            onConfirmBrief={confirmBrief}
            onRejectBrief={rejectBrief}
            onResolveCardApproval={resolveChatApproval}
            onAnswerCardClarification={answerChatClarification}
          />
        )}

        {view === 'chat' && !activeChat && (
          <EmptyWorkbenchState
            title={t('chat.noConversation')}
            text={t('chat.noConversationHint')}
          />
        )}

        {view === 'staff' && (
          <StaffView
            companyName={workspace.name}
            departments={departments}
            agents={agents}
            tasks={tasks}
            busyCount={busyCount}
            onOpenCreate={openCreateAgent}
            onOpenTeamCompiler={openTeamCompiler}
            onOpenAgent={(id) => setDetailId(id)}
          />
        )}

        {view === 'market' && (
          <TalentMarketView
            agents={agents}
            templates={hireTemplates}
            categories={talentCategories}
            skillCount={allSkills.length}
            mcpCount={allMcps.length}
            onRecruit={openHire}
          />
        )}

        {view === 'tasks' && (
          <TasksView
            tasks={filteredTasks}
            tabs={taskTabs}
            activeFilter={taskFilter}
            agents={agents}
            scopeLabel={taskScopeLabel}
            onPickFilter={setTaskFilter}
            onClearScope={openAllTasks}
            onOpenCreateTask={openCreateTask}
            onAdvanceTask={advanceTask}
            onOpenClaimTask={(taskId) => {
              const task = tasks.find((item) => item.id === taskId);
              setClaimTaskId(taskId);
              setClaimAgentId(task?.suggestedAgentId ?? '');
            }}
            onOpenTask={(taskId) => setTaskDetailId(taskId)}
            onOpenChat={openChat}
            onOpenAgent={(id) => setDetailId(id)}
          />
        )}

        {view === 'lib' && (
          <LibraryView
            tabs={libraryTabs}
            activeTab={libraryTab}
            onPickTab={setLibraryTab}
            knowledgeSources={knowledgeSources}
            skills={allSkills}
            mcps={allMcps}
            onOpenKnowledge={() => setKnowledgeOpen(true)}
            onSyncObsidian={syncObsidian}
            obsidianSyncing={obsidianSyncing}
          />
        )}

        {view === 'ideas' && token && (
          <IdeasView
            token={token}
            agents={agents}
            onConverted={async (conversationId) => {
              await loadBootstrap(token);
              openChat(conversationId);
              showToast('已根据想法拉起讨论');
            }}
          />
        )}

        {view === 'channels' && token && (
          <ChannelsView token={token} agents={agents} />
        )}

        {view === 'settings' && (
          <ModelProviderSettings
            status={modelProvider}
            loading={modelProviderLoading}
            onConfigure={() => {
              setModelProviderError('');
              setModelProviderOpen(true);
            }}
            onRevoke={revokeModelProvider}
          />
        )}
      </section>

      {detailAgent && (
        <AgentDetail
          agent={detailAgent}
          tasks={tasks.filter((task) => task.owner === detailAgent.id)}
          token={token}
          onClose={() => setDetailId(null)}
          onDm={() => {
            const chat = chats.find(
              (item) => item.kind === 'dm' && item.agentId === detailAgent.id,
            );
            if (chat) openChat(chat.id);
          }}
        />
      )}

      {detailTask && (
        <TaskDetail
          task={detailTask}
          token={token}
          plan={
            detailTask.taskPlanId
              ? taskPlans[detailTask.taskPlanId]
              : undefined
          }
          agent={detailTask.owner ? agentById(detailTask.owner) : undefined}
          onClose={() => setTaskDetailId(null)}
          onOpenChat={openChat}
          onOpenAgent={(id) => setDetailId(id)}
          onAdvanceTask={advanceTask}
          onResolveApproval={resolveApproval}
        />
      )}

      {hireOpen && (
        <HireModal
          template={hireTemplates.find((template) => template.id === hireTpl)}
          departments={departments
            .filter((dept) => dept.name !== '老板办公室')
            .map((dept) => dept.name)}
          hireDept={hireDept}
          onDeptChange={setHireDept}
          onClose={() => setHireOpen(false)}
          onSubmit={submitHire}
        />
      )}

      {createOpen && (
        <CreateAgentModal
          token={token ?? ''}
          departments={departments
            .filter((dept) => dept.name !== '老板办公室')
            .map((dept) => dept.name)}
          createName={createName}
          createDesc={createDesc}
          createDept={createDept}
          createPrompt={createPrompt}
          createCapKeys={createCapKeys}
          onNameChange={setCreateName}
          onDescChange={setCreateDesc}
          onDeptChange={setCreateDept}
          onPromptChange={setCreatePrompt}
          onCapKeysChange={setCreateCapKeys}
          onClose={() => setCreateOpen(false)}
          onSubmit={submitCreateAgent}
        />
      )}

      {teamCompilerOpen && (
        <TeamCompilerModal
          token={token ?? ''}
          description={teamDescription}
          onDescriptionChange={setTeamDescription}
          drafts={teamDrafts}
          loading={teamDraftLoading}
          error={teamDraftError}
          creating={teamCreating}
          onDraft={draftTeamFromDescription}
          onUpdateMember={updateTeamDraftMember}
          onRemoveMember={removeTeamDraftMember}
          onBack={() => setTeamDrafts(null)}
          onConfirm={confirmCreateTeam}
          onClose={() => setTeamCompilerOpen(false)}
        />
      )}

      {groupOpen && (
        <GroupModal
          agents={agents}
          tasks={tasks}
          groupName={groupName}
          groupMembers={groupMembers}
          selectedTaskIds={groupTaskIds}
          onGroupNameChange={setGroupName}
          onToggleMember={(id) =>
            setGroupMembers((current) =>
              current.includes(id)
                ? current.filter((memberId) => memberId !== id)
                : [...current, id],
            )
          }
          onToggleTask={(id) =>
            setGroupTaskIds((current) =>
              current.includes(id)
                ? current.filter((taskId) => taskId !== id)
                : [...current, id],
            )
          }
          onClose={() => setGroupOpen(false)}
          onCreate={createGroup}
        />
      )}

      {inviteOpen && activeChat?.kind === 'group' && (
        <GroupMembersModal
          title={t('chat.inviteMembers')}
          description={t('groupMembersModal.inviteDescription')}
          agents={agents.filter(
            (agent) => !activeChat.memberIds.includes(agent.id),
          )}
          selectedMembers={inviteMembers}
          submitLabel={t('groupMembersModal.inviteSubmit')}
          emptyText={t('groupMembersModal.allInvited')}
          onToggleMember={(id) =>
            setInviteMembers((current) =>
              current.includes(id)
                ? current.filter((memberId) => memberId !== id)
                : [...current, id],
            )
          }
          onClose={() => setInviteOpen(false)}
          onSubmit={inviteGroupMembers}
        />
      )}

      {taskOpen && (
        <CreateTaskModal
          agents={agents}
          chats={chats}
          taskTitle={taskTitle}
          taskDesc={taskDesc}
          taskPriority={taskPriority}
          taskOwnerId={taskOwnerId}
          taskConversationId={taskConversationId}
          agentById={agentById}
          onTitleChange={setTaskTitle}
          onDescChange={setTaskDesc}
          onPriorityChange={setTaskPriority}
          onOwnerChange={setTaskOwnerId}
          onConversationChange={setTaskConversationId}
          onClose={() => setTaskOpen(false)}
          onSubmit={submitCreateTask}
        />
      )}

      {claimTaskId && (
        <ClaimTaskModal
          task={tasks.find((task) => task.id === claimTaskId)}
          agents={agents}
          selectedAgentId={claimAgentId}
          onAgentChange={setClaimAgentId}
          onClose={() => {
            setClaimTaskId(null);
            setClaimAgentId('');
          }}
          onSubmit={claimTask}
        />
      )}

      {knowledgeOpen && (
        <KnowledgeSourceModal
          title={knowledgeTitle}
          category={knowledgeCategory}
          content={knowledgeContent}
          onTitleChange={setKnowledgeTitle}
          onCategoryChange={setKnowledgeCategory}
          onContentChange={setKnowledgeContent}
          onClose={() => setKnowledgeOpen(false)}
          onSubmit={submitKnowledgeSource}
        />
      )}

      {onboardingOpen && (
        <OnboardingModal
          step={onboardingStep}
          templates={hireTemplates}
          onNext={() =>
            setOnboardingStep((current) => Math.min(2, current + 1))
          }
          onFinish={finishOnboarding}
        />
      )}

      {modelProviderOpen && (
        <ModelProviderModal
          saving={modelProviderSaving}
          error={modelProviderError}
          onClose={() => {
            if (!modelProviderSaving) setModelProviderOpen(false);
          }}
          onSave={saveModelProvider}
        />
      )}

      {toast.visible && <div className="toast">{toast.message}</div>}
    </main>
  );
}

function mapApiMessage(message: {
  id: string;
  sender_type: 'user' | 'agent' | 'system';
  sender_id: string;
  content: string;
  created_at: string;
  provider?: string | null;
  model?: string | null;
}): Message {
  return {
    id: message.id,
    from:
      message.sender_type === 'user'
        ? 'boss'
        : message.sender_type === 'system'
          ? 'system'
          : message.sender_id,
    type: message.sender_type === 'system' ? 'system' : 'text',
    time: formatTime(message.created_at),
    text: message.content,
    provider: message.provider ?? undefined,
    model: message.model ?? undefined,
  };
}

function AuthScreen({
  theme,
  error,
  onAuthSuccess,
}: {
  theme: EffectiveTheme;
  error: string;
  onAuthSuccess: (payload: {
    access_token: string;
    user: User;
    workspace: Workspace;
  }) => void;
}) {
  const { t } = useTranslation();
  const [mode, setMode] = useState<'login' | 'register'>('register');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [workspaceName, setWorkspaceName] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [localError, setLocalError] = useState(error);

  const submit = async () => {
    const nextEmail = email.trim();
    const nextPassword = password.trim();
    const nextDisplayName = displayName.trim();
    const nextWorkspaceName = workspaceName.trim();

    if (!nextEmail) {
      setLocalError(t('auth.emailRequired'));
      return;
    }
    if (mode === 'register' && nextPassword.length < 10) {
      setLocalError(t('auth.passwordMinLength'));
      return;
    }
    if (mode === 'login' && !nextPassword) {
      setLocalError(t('auth.passwordRequired'));
      return;
    }
    if (mode === 'register' && !nextDisplayName) {
      setLocalError(t('auth.displayNameRequired'));
      return;
    }
    if (mode === 'register' && !nextWorkspaceName) {
      setLocalError(t('auth.workspaceNameRequired'));
      return;
    }

    setSubmitting(true);
    setLocalError('');
    try {
      const path = mode === 'register' ? '/auth/register' : '/auth/login';
      const body =
        mode === 'register'
          ? {
              email: nextEmail,
              password: nextPassword,
              display_name: nextDisplayName,
              workspace_name: nextWorkspaceName,
            }
          : { email: nextEmail, password: nextPassword };
      const payload = await apiRequest<{
        access_token: string;
        user: User;
        workspace: Workspace;
      }>(path, {
        method: 'POST',
        body: JSON.stringify(body),
      });
      await onAuthSuccess(payload);
    } catch (authError) {
      setLocalError(
        authError instanceof Error ? authError.message : t('auth.loginFailed'),
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="workbench-shell auth-shell" data-theme={theme}>
      <section className="auth-hero" aria-label="AgentPulse 介绍">
        <div className="auth-brand">
          <div className="auth-mark" aria-hidden="true">
            <svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
              <path d="M16 4 L27 26 H21.5 L16 14 L10.5 26 H5 Z" fill="url(#authMarkGradient)" />
              <rect x="10.5" y="18" width="11" height="4.5" rx="2.25" fill="#062b26" fillOpacity=".55" />
              <circle cx="16" cy="20.25" r="2.25" fill="#0d9488" />
              <defs>
                <linearGradient id="authMarkGradient" x1="5" y1="4" x2="27" y2="26">
                  <stop stopColor="#2dd4bf" />
                  <stop offset="1" stopColor="#0d9488" />
                </linearGradient>
              </defs>
            </svg>
          </div>
          <span>AgentPulse</span>
        </div>
        <h1>{t('auth.heroTitle')}</h1>
        <p>{t('auth.heroSubtitle')}</p>
        <div className="auth-feature-list">
          <div>
            {materialIcon('account_tree')}
            <span>
              <strong>{t('auth.feature1Title')}</strong>
              <em>{t('auth.feature1Desc')}</em>
            </span>
          </div>
          <div>
            {materialIcon('forum')}
            <span>
              <strong>{t('auth.feature2Title')}</strong>
              <em>{t('auth.feature2Desc')}</em>
            </span>
          </div>
          <div>
            {materialIcon('verified')}
            <span>
              <strong>{t('auth.feature3Title')}</strong>
              <em>{t('auth.feature3Desc')}</em>
            </span>
          </div>
        </div>
      </section>

      <section className="auth-panel">
        <div className="auth-panel-header">
          <span>{mode === 'register' ? t('auth.registerEyebrow') : t('auth.loginEyebrow')}</span>
          <h2>{mode === 'register' ? t('auth.registerTitle') : t('auth.loginTitle')}</h2>
          <p>
            {mode === 'register' ? t('auth.registerSubtitle') : t('auth.loginSubtitle')}
          </p>
        </div>

        <div className="auth-tabs">
          <button
            className={mode === 'register' ? 'active' : ''}
            type="button"
            onClick={() => {
              setMode('register');
              setLocalError('');
            }}
          >
            {t('auth.tabRegister')}
          </button>
          <button
            className={mode === 'login' ? 'active' : ''}
            type="button"
            onClick={() => {
              setMode('login');
              setLocalError('');
            }}
          >
            {t('auth.tabLogin')}
          </button>
        </div>

        <div className="auth-form">
          <label>
            <span>{t('auth.email')}</span>
            <input
              value={email}
              placeholder="you@example.com"
              onChange={(event) => setEmail(event.target.value)}
            />
          </label>
          <label>
            <span>{t('auth.password')}</span>
            <input
              type="password"
              value={password}
              placeholder={t('auth.passwordPlaceholder')}
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>
          {mode === 'register' && (
            <>
              <label>
                <span>{t('auth.displayName')}</span>
                <input
                  value={displayName}
                  placeholder={t('auth.displayNamePlaceholder')}
                  onChange={(event) => setDisplayName(event.target.value)}
                />
              </label>
              <label>
                <span>{t('auth.workspaceName')}</span>
                <input
                  value={workspaceName}
                  placeholder={t('auth.workspaceNamePlaceholder')}
                  onChange={(event) => setWorkspaceName(event.target.value)}
                />
              </label>
            </>
          )}
        </div>

        {(localError || error) && (
          <div className="auth-error">{localError || error}</div>
        )}
        <button
          className="button primary auth-submit"
          type="button"
          onClick={submit}
          disabled={submitting}
        >
          {submitting
            ? t('auth.submitting')
            : mode === 'register'
              ? t('auth.submitRegister')
              : t('auth.submitLogin')}
        </button>
      </section>
    </main>
  );
}

function Sidebar({
  view,
  unreadTotal,
  taskAlerts,
  anomalyCount24h,
  themeMode,
  onThemeModeChange,
  onLogout,
  onNavigate,
}: {
  view: View;
  unreadTotal: number;
  taskAlerts: number;
  anomalyCount24h: number;
  themeMode: ThemeMode;
  onThemeModeChange: (themeMode: ThemeMode) => void;
  onLogout: () => void;
  onNavigate: (view: View) => void;
}) {
  const { t } = useTranslation();
  const [language, setLanguage] = useState<AppLanguage>(getAppLanguage);

  const items: Array<{
    key: View;
    icon: string;
    label: string;
    badge: number;
  }> = [
    { key: 'chat', icon: 'forum', label: t('nav.chat'), badge: unreadTotal },
    { key: 'staff', icon: 'group', label: t('nav.staff'), badge: 0 },
    { key: 'market', icon: 'storefront', label: t('nav.market'), badge: 0 },
    { key: 'tasks', icon: 'task_alt', label: t('nav.tasks'), badge: taskAlerts },
    { key: 'ideas', icon: 'lightbulb', label: t('nav.ideas'), badge: 0 },
    { key: 'channels', icon: 'hub', label: t('nav.channels'), badge: 0 },
    { key: 'lib', icon: 'folder_open', label: t('nav.lib'), badge: 0 },
    { key: 'settings', icon: 'settings', label: t('nav.settings'), badge: 0 },
  ];

  const toggleLanguage = () => {
    const next: AppLanguage = language === 'zh' ? 'en' : 'zh';
    setAppLanguage(next);
    setLanguage(next);
  };

  return (
    <aside className="sidebar">
      <div
        className="brand-mark"
        title={
          anomalyCount24h > 0
            ? t('nav.anomalyTooltip', { count: anomalyCount24h })
            : undefined
        }
      >
        <svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
          <path d="M16 4 L27 26 H21.5 L16 14 L10.5 26 H5 Z" fill="#06090c" fillOpacity=".92" />
          <rect x="10.5" y="18" width="11" height="4.5" rx="2.25" fill="#0d9488" />
          <circle cx="16" cy="20.25" r="2.25" fill="#eef4f2" />
        </svg>
        {anomalyCount24h > 0 && (
          <em className="brand-mark-anomaly">{anomalyCount24h}</em>
        )}
      </div>
      {items.map((item) => (
        <button
          className={view === item.key ? 'nav-item active' : 'nav-item'}
          key={item.key}
          type="button"
          onClick={() => onNavigate(item.key)}
          aria-label={item.label}
        >
          {materialIcon(item.icon)}
          <span>{item.label}</span>
          {item.badge > 0 && <em>{item.badge}</em>}
        </button>
      ))}
      <div className="sidebar-spacer" />
      <button
        className="language-switch-button"
        type="button"
        title={t('nav.language')}
        aria-label={t('nav.language')}
        onClick={toggleLanguage}
      >
        {language === 'zh' ? 'EN' : '中'}
      </button>
      <div className="theme-switcher" aria-label={t('nav.themeSettings')}>
        {themeOptions.map((option) => (
          <button
            className={themeMode === option.mode ? 'active' : ''}
            key={option.mode}
            type="button"
            title={t(option.labelKey)}
            aria-label={t(option.labelKey)}
            onClick={() => onThemeModeChange(option.mode)}
          >
            {materialIcon(option.icon)}
          </button>
        ))}
      </div>
      <button
        className="logout-nav-button"
        type="button"
        title={t('nav.logout')}
        aria-label={t('nav.logout')}
        onClick={onLogout}
      >
        <span className="owner-avatar">我</span>
        {materialIcon('logout')}
      </button>
    </aside>
  );
}

type Idea = {
  id: string;
  source_agent_id: string;
  source_agent_name: string;
  title: string;
  description: string;
  category: string;
  status: string;
  converted_brief_id: string | null;
  created_at: string;
  reviewed_at: string | null;
};

const IDEA_CATEGORIES: Record<string, { key: string; color: string }> = {
  improvement: { key: 'improvement', color: 'var(--primary)' },
  opportunity: { key: 'opportunity', color: 'var(--success)' },
  risk: { key: 'risk', color: 'var(--danger)' },
  learning: { key: 'learning', color: 'var(--warning)' },
};

function IdeasView({
  token,
  agents,
  onConverted,
}: {
  token: string;
  agents: Agent[];
  onConverted: (conversationId: string) => void;
}) {
  const { t } = useTranslation();
  const [ideas, setIdeas] = useState<Idea[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [filter, setFilter] = useState('all');

  useEffect(() => {
    let alive = true;
    setLoading(true);
    apiRequest<Idea[]>('/ideas', { token })
      .then((data) => {
        if (alive) setIdeas(data);
      })
      .catch((err: unknown) => {
        if (alive) setError(err instanceof Error ? err.message : t('ideas.loadFailed'));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [token, t]);

  const review = async (id: string, action: 'accept' | 'dismiss') => {
    try {
      const updated = await apiRequest<Idea>(`/ideas/${id}/review`, {
        method: 'POST',
        token,
        body: JSON.stringify({ action }),
      });
      setIdeas((prev) => prev.map((item) => (item.id === id ? updated : item)));
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : t('ideas.actionFailed'));
    }
  };

  const convert = async (id: string) => {
    try {
      const res = await apiRequest<{ conversation_id: string; idea: Idea }>(
        `/ideas/${id}/convert`,
        { method: 'POST', token },
      );
      setIdeas((prev) => prev.map((item) => (item.id === id ? res.idea : item)));
      onConverted(res.conversation_id);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : t('ideas.convertFailed'));
    }
  };

  const agentColor = (id: string) => {
    const agent = agents.find((item) => item.id === id);
    return agent ? avatarColor(agent) : 'var(--primary)';
  };

  const filtered =
    filter === 'all' ? ideas : ideas.filter((idea) => idea.category === filter);
  const summary = ideas.length
    ? t('ideas.summary', {
        count: ideas.length,
        agentCount: new Set(ideas.map((i) => i.source_agent_id)).size,
      })
    : t('ideas.summaryEmpty');

  return (
    <div className="screen-scroll">
      <div className="screen-inner">
        <header className="page-header">
          <div>
            <h1>{t('ideas.title')}</h1>
            <p>{summary}</p>
          </div>
        </header>

        <div className="tabs">
          {[
            { key: 'all', label: t('ideas.filterAll') },
            ...Object.entries(IDEA_CATEGORIES).map(([key, meta]) => ({
              key,
              label: t(`ideas.category.${meta.key}`),
            })),
          ].map((tab) => (
            <button
              className={filter === tab.key ? 'tab active' : 'tab'}
              key={tab.key}
              type="button"
              onClick={() => setFilter(tab.key)}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {error && <div className="auth-error">{error}</div>}

        {loading ? (
          <div className="empty-state">{t('common.loading')}</div>
        ) : filtered.length === 0 ? (
          <div className="empty-state">{t('ideas.empty')}</div>
        ) : (
          <div className="idea-list">
            {filtered.map((idea) => {
              const cat = IDEA_CATEGORIES[idea.category];
              const catLabel = cat ? t(`ideas.category.${cat.key}`) : idea.category;
              const catColor = cat?.color ?? 'var(--muted)';
              return (
                <article className="card idea-card" key={idea.id}>
                  <div className="idea-head">
                    <span
                      className="idea-avatar"
                      style={{ background: agentColor(idea.source_agent_id) }}
                    >
                      {avatarText(idea.source_agent_name)}
                    </span>
                    <div className="idea-byline">
                      <strong>{idea.source_agent_name}</strong>
                      <em>{formatTime(idea.created_at)}</em>
                    </div>
                    <span
                      className="idea-cat"
                      style={{ background: catColor + '1f', color: catColor }}
                    >
                      {catLabel}
                    </span>
                    {idea.status !== 'new' && (
                      <span className="idea-status-tag">
                        {t(`ideas.status.${idea.status}`, { defaultValue: idea.status })}
                      </span>
                    )}
                  </div>
                  <h3 className="idea-title">{idea.title}</h3>
                  <p className="idea-desc">{idea.description}</p>
                  {idea.status === 'new' && (
                    <div className="idea-actions">
                      <button
                        className="small-button primary"
                        type="button"
                        onClick={() => convert(idea.id)}
                      >
                        {materialIcon('forum')}{t('ideas.convertToDiscussion')}
                      </button>
                      <button
                        className="small-button"
                        type="button"
                        onClick={() => review(idea.id, 'accept')}
                      >
                        {t('ideas.accept')}
                      </button>
                      <button
                        className="small-button"
                        type="button"
                        onClick={() => review(idea.id, 'dismiss')}
                      >
                        {t('ideas.dismiss')}
                      </button>
                    </div>
                  )}
                </article>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

type ChannelConfig = {
  id: string;
  channel_type: string;
  name: string;
  token: string;
  config: Record<string, unknown> & {
    provider?: string;
    from_address?: string;
    from_name?: string;
  };
  target_agent_id: string | null;
  target_conversation_id: string | null;
  active: boolean;
  created_at: string;
  webhook_url: string;
};

const CHANNEL_TYPES: Array<{ value: string; labelKey: string }> = [
  { value: 'generic_webhook', labelKey: 'channels.type.generic_webhook' },
  { value: 'email', labelKey: 'channels.type.email' },
  { value: 'web_widget', labelKey: 'channels.type.web_widget' },
  { value: 'wechat', labelKey: 'channels.type.wechat' },
];

function channelTypeLabel(type: string, t: (key: string, opts?: Record<string, unknown>) => string) {
  const match = CHANNEL_TYPES.find((item) => item.value === type);
  return match ? t(match.labelKey) : type;
}

function ChannelsView({ token, agents }: { token: string; agents: Agent[] }) {
  const { t } = useTranslation();
  const [channels, setChannels] = useState<ChannelConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [name, setName] = useState('');
  const [channelType, setChannelType] = useState('generic_webhook');
  const [targetAgentId, setTargetAgentId] = useState('');
  const [fromAddress, setFromAddress] = useState('');
  const [fromName, setFromName] = useState('');
  const [creating, setCreating] = useState(false);
  const [copiedId, setCopiedId] = useState('');

  const webhookOrigin = apiBaseUrl.replace(/\/api\/?$/, '');
  const fullUrl = (path: string) => `${webhookOrigin}${path}`;

  useEffect(() => {
    let alive = true;
    setLoading(true);
    apiRequest<ChannelConfig[]>('/channels', { token })
      .then((data) => {
        if (alive) setChannels(data);
      })
      .catch((err: unknown) => {
        if (alive) setError(err instanceof Error ? err.message : t('channels.loadFailed'));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [token, t]);

  const create = async () => {
    if (!name.trim() || creating) return;
    setCreating(true);
    setError('');
    try {
      const created = await apiRequest<ChannelConfig>('/channels', {
        method: 'POST',
        token,
        body: JSON.stringify({
          channel_type: channelType,
          name: name.trim(),
          target_agent_id: targetAgentId || null,
          config:
            channelType === 'email'
              ? {
                  provider: 'resend',
                  from_address: fromAddress.trim(),
                  from_name: fromName.trim(),
                }
              : {},
        }),
      });
      setChannels((prev) => [created, ...prev]);
      setName('');
      setTargetAgentId('');
      setFromAddress('');
      setFromName('');
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : t('channels.createFailed'));
    } finally {
      setCreating(false);
    }
  };

  const deactivate = async (id: string) => {
    setError('');
    try {
      const updated = await apiRequest<ChannelConfig>(`/channels/${id}`, {
        method: 'DELETE',
        token,
      });
      setChannels((prev) => prev.map((item) => (item.id === id ? updated : item)));
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : t('channels.actionFailed'));
    }
  };

  const copyUrl = async (channel: ChannelConfig) => {
    try {
      await navigator.clipboard.writeText(fullUrl(channel.webhook_url));
      setCopiedId(channel.id);
      window.setTimeout(() => setCopiedId(''), 1500);
    } catch {
      /* clipboard blocked — user can select the text manually */
    }
  };

  const agentName = (id: string | null) =>
    id ? (agents.find((agent) => agent.id === id)?.name ?? id) : t('channels.byRouting');

  return (
    <div className="screen-scroll">
      <div className="screen-inner">
        <header className="page-header">
          <div>
            <h1>{t('channels.title')}</h1>
            <p>{t('channels.subtitle')}</p>
          </div>
        </header>

        <section className="card channel-create" aria-label={t('channels.createAria')}>
          <div className="channel-form-row">
            <label className="channel-field">
              <span>{t('channels.name')}</span>
              <input
                value={name}
                placeholder={t('channels.namePlaceholder')}
                onChange={(event) => setName(event.target.value)}
              />
            </label>
            <label className="channel-field">
              <span>{t('channels.typeLabel')}</span>
              <select
                value={channelType}
                onChange={(event) => setChannelType(event.target.value)}
              >
                {CHANNEL_TYPES.map((item) => (
                  <option key={item.value} value={item.value}>
                    {t(item.labelKey)}
                  </option>
                ))}
              </select>
            </label>
            <label className="channel-field">
              <span>{t('channels.defaultAgent')}</span>
              <select
                value={targetAgentId}
                onChange={(event) => setTargetAgentId(event.target.value)}
              >
                <option value="">{t('channels.byRoutingOption')}</option>
                {agents.map((agent) => (
                  <option key={agent.id} value={agent.id}>
                    {agent.name}
                  </option>
                ))}
              </select>
            </label>
            {channelType === 'email' && (
              <>
                <label className="channel-field">
                  <span>发件邮箱</span>
                  <input
                    type="email"
                    value={fromAddress}
                    placeholder="hello@your-domain.com"
                    onChange={(event) => setFromAddress(event.target.value)}
                  />
                </label>
                <label className="channel-field">
                  <span>发件名称</span>
                  <input
                    value={fromName}
                    placeholder="品牌名称"
                    onChange={(event) => setFromName(event.target.value)}
                  />
                </label>
              </>
            )}
            <button
              className="button primary"
              type="button"
              onClick={create}
              disabled={
                creating ||
                !name.trim() ||
                (channelType === 'email' && (!targetAgentId || !fromAddress.trim()))
              }
            >
              {materialIcon('add_link')}
              {creating ? t('channels.creating') : t('channels.create')}
            </button>
          </div>
        </section>

        {error && <div className="auth-error">{error}</div>}

        {loading ? (
          <div className="empty-state">{t('common.loading')}</div>
        ) : channels.length === 0 ? (
          <div className="empty-state">{t('channels.empty')}</div>
        ) : (
          <div className="channel-list">
            {channels.map((channel) => (
              <article className="card channel-card" key={channel.id}>
                <div className="channel-card-head">
                  <strong>{channel.name}</strong>
                  <span className="channel-type-tag">
                    {channelTypeLabel(channel.channel_type, t)}
                  </span>
                  <span
                    className={channel.active ? 'channel-status on' : 'channel-status off'}
                  >
                    <i className="channel-dot" />
                    {channel.active ? t('channels.active') : t('channels.inactive')}
                  </span>
                </div>
                <div className="channel-url">
                  {materialIcon('link')}
                  <code>{fullUrl(channel.webhook_url)}</code>
                  <button
                    className="small-button"
                    type="button"
                    onClick={() => copyUrl(channel)}
                  >
                    {copiedId === channel.id ? t('channels.copied') : t('channels.copy')}
                  </button>
                </div>
                <div className="channel-meta">
                  <span>{t('channels.defaultAgentLabel', { name: agentName(channel.target_agent_id) })}</span>
                  {channel.channel_type === 'email' && channel.config.from_address && (
                    <span>{String(channel.config.from_name || channel.config.from_address)} · {String(channel.config.from_address)}</span>
                  )}
                  {channel.active && (
                    <button
                      className="channel-link-button"
                      type="button"
                      onClick={() => deactivate(channel.id)}
                    >
                      {t('channels.deactivate')}
                    </button>
                  )}
                </div>
              </article>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function ConversationList({
  chats,
  activeChatId,
  agents,
  lastMessagePreview,
  onOpenChat,
  onOpenGroupModal,
}: {
  chats: Chat[];
  activeChatId: string;
  agents: Agent[];
  lastMessagePreview: (chatId: string) => string;
  onOpenChat: (id: string) => void;
  onOpenGroupModal: () => void;
}) {
  const { t } = useTranslation();
  const agentById = (id: string) => agents.find((agent) => agent.id === id);
  const pinnedChats = chats.filter(
    (chat) =>
      chat.kind === 'dm' && agentById(chat.agentId)?.role === '老板秘书',
  );
  const groupChats = chats.filter((chat) => chat.kind === 'group');
  const dmChats = chats.filter(
    (chat) =>
      chat.kind === 'dm' && agentById(chat.agentId)?.role !== '老板秘书',
  );

  const renderChat = (chat: Chat) => {
    const agent = chat.kind === 'dm' ? agentById(chat.agentId) : null;
    const name =
      chat.kind === 'dm'
        ? `${agent?.name ?? ''} · ${agent?.role ?? ''}`
        : chat.name;

    return (
      <button
        className={
          activeChatId === chat.id ? 'chat-list-item active' : 'chat-list-item'
        }
        key={chat.id}
        type="button"
        onClick={() => onOpenChat(chat.id)}
      >
        <div
          className={
            chat.kind === 'group' ? 'chat-avatar group' : 'chat-avatar'
          }
          style={
            chat.kind === 'dm' && agent
              ? { background: avatarColor(agent) }
              : undefined
          }
        >
          {chat.kind === 'group' ? '#' : agent ? avatarText(agent.name) : ''}
        </div>
        <div className="chat-copy">
          <div className="chat-line">
            <strong>{name}</strong>
            <span>{chat.time}</span>
          </div>
          <div className="chat-subline">
            <p>{lastMessagePreview(chat.id)}</p>
            {chat.unread > 0 && <em>{chat.unread}</em>}
          </div>
        </div>
      </button>
    );
  };

  return (
    <aside className="conversation-list">
      <div className="conversation-title">
        <strong>{t('nav.chat')}</strong>
        <button type="button" title={t('chat.groupDiscussion')} onClick={onOpenGroupModal}>
          {materialIcon('group_add')}
        </button>
      </div>
      <div className="search-box">
        {materialIcon('search')}
        <span>{t('chat.searchPlaceholder')}</span>
      </div>
      <div className="conversation-scroll">
        <SectionLabel label="秘书" />
        {pinnedChats.map(renderChat)}
        <SectionLabel label="群组" />
        {groupChats.length ? (
          groupChats.map(renderChat)
        ) : (
          <EmptyState>暂无群聊</EmptyState>
        )}
        <SectionLabel label="私聊" />
        {dmChats.length ? (
          dmChats.map(renderChat)
        ) : (
          <EmptyState>暂无其他私聊</EmptyState>
        )}
      </div>
    </aside>
  );
}

function SectionLabel({ label }: { label: string }) {
  return <div className="section-label">{label}</div>;
}

function EmptyState({ children }: { children: string }) {
  return <div className="empty-state">{children}</div>;
}

function EmptyWorkbenchState({ title, text }: { title: string; text: string }) {
  return (
    <div className="screen-scroll">
      <div className="screen-inner">
        <article className="card empty-workbench-state">
          <h2>{title}</h2>
          <p>{text}</p>
        </article>
      </div>
    </div>
  );
}

function ChatView({
  conversationId,
  token,
  title,
  meta,
  members,
  messages,
  agents,
  approvals,
  relatedTasks,
  relatedTaskCount,
  draft,
  placeholder,
  typingName,
  messagesRef,
  onDraftChange,
  onSend,
  onOpenTasks,
  onResolveApproval,
  onOpenTask,
  onInviteMembers,
  onOpenAgent,
  onConfirmBrief,
  onRejectBrief,
  onResolveCardApproval,
  onAnswerCardClarification,
}: {
  conversationId: string;
  token: string | null;
  title: string;
  meta: string;
  members: string[];
  messages: Message[];
  agents: Agent[];
  approvals: Array<{ approval: Approval; task: Task }>;
  relatedTasks: Task[];
  relatedTaskCount: number;
  draft: string;
  placeholder: string;
  typingName: string | null;
  messagesRef: RefObject<HTMLDivElement | null>;
  onDraftChange: (draft: string) => void;
  onSend: () => void;
  onOpenTasks: () => void;
  onResolveApproval: (
    approval: Approval,
    status: 'approved' | 'rejected',
  ) => void;
  onOpenTask: (taskId: string) => void;
  onInviteMembers?: () => void;
  onOpenAgent: (id: string) => void;
  onConfirmBrief?: (briefId: string) => Promise<boolean>;
  onRejectBrief?: (briefId: string) => Promise<boolean>;
  onResolveCardApproval?: (
    approvalId: string,
    status: 'approved' | 'rejected',
    approvedCapabilityKey?: string,
    scope?: 'once' | 'always',
  ) => Promise<boolean>;
  onAnswerCardClarification?: (
    approvalId: string,
    answer: string,
  ) => Promise<boolean>;
}) {
  const { t } = useTranslation();
  const inputRef = useRef<HTMLInputElement>(null);
  const [runTraceOpen, setRunTraceOpen] = useState(false);
  const [mentionOpen, setMentionOpen] = useState(false);
  const [mentionQuery, setMentionQuery] = useState('');
  const [mentionStart, setMentionStart] = useState<number | null>(null);
  const [mentionIndex, setMentionIndex] = useState(0);
  const agentById = (id: string) => agents.find((agent) => agent.id === id);
  const mentionableAgents = useMemo(
    () =>
      members
        .map((memberId) => agents.find((agent) => agent.id === memberId))
        .filter((agent): agent is Agent => Boolean(agent)),
    [agents, members],
  );
  const mentionMatches = useMemo(() => {
    const query = mentionQuery.trim().toLocaleLowerCase();
    return mentionableAgents
      .filter((agent) => {
        if (!query) return true;
        return [agent.name, agent.role, agent.dept]
          .join(' ')
          .toLocaleLowerCase()
          .includes(query);
      })
      .slice(0, 6);
  }, [mentionQuery, mentionableAgents]);

  const closeMention = () => {
    setMentionOpen(false);
    setMentionQuery('');
    setMentionStart(null);
    setMentionIndex(0);
  };

  const updateMentionState = (value: string, cursor: number) => {
    const context = getMentionContext(value, cursor);
    if (!context) {
      closeMention();
      return;
    }
    setMentionOpen(true);
    setMentionQuery(context.query);
    setMentionStart(context.start);
    setMentionIndex(0);
  };

  const insertMention = (agent: Agent) => {
    const cursor = inputRef.current?.selectionStart ?? draft.length;
    const context =
      mentionStart === null
        ? getMentionContext(draft, cursor)
        : { query: mentionQuery, start: mentionStart };
    if (!context) return;

    const before = draft.slice(0, context.start);
    const after = draft.slice(cursor).replace(/^\s*/, '');
    const nextDraft = `${before}@${agent.name} ${after}`;
    const nextCursor = `${before}@${agent.name} `.length;

    onDraftChange(nextDraft);
    closeMention();
    window.requestAnimationFrame(() => {
      inputRef.current?.focus();
      inputRef.current?.setSelectionRange(nextCursor, nextCursor);
    });
  };

  return (
    <div className="chat-view">
      <header className="chat-header">
        <div>
          <h2>{title}</h2>
          <p>{meta}</p>
        </div>
        <div className="chat-members">
          {members.map((memberId) => {
            const agent = agentById(memberId);
            if (!agent) return null;
            return (
              <button
                key={memberId}
                style={{ background: avatarColor(agent) }}
                title={`${agent.name} · ${t('chat.viewStatus')}`}
                type="button"
                onClick={() => onOpenAgent(memberId)}
              >
                {avatarText(agent.name)}
              </button>
            );
          })}
        </div>
        {onInviteMembers && (
          <button
            className="chat-icon-button"
            title={t('chat.inviteMembers')}
            type="button"
            onClick={onInviteMembers}
          >
            {materialIcon('person_add')}
          </button>
        )}
        <button
          className="related-task-button"
          type="button"
          onClick={onOpenTasks}
        >
          {materialIcon('task_alt')}
          {relatedTaskCount
            ? t('chat.relatedTasksWithCount', { count: relatedTaskCount })
            : t('chat.relatedTasks')}
        </button>
        <button
          className="related-task-button"
          type="button"
          onClick={() => setRunTraceOpen(true)}
        >
          {materialIcon('timeline')}
          {t('chat.runTrace')}
        </button>
      </header>

      {runTraceOpen && (
        <RunTraceModal
          conversationId={conversationId}
          token={token}
          agents={agents}
          onClose={() => setRunTraceOpen(false)}
        />
      )}

      {relatedTasks.length > 0 && (
        <div className="chat-task-rail">
          {relatedTasks.map((task) => {
            const owner = agentById(task.owner);
            const status = statusStyle(task.status);
            return (
              <button
                className="chat-task-chip"
                key={task.id}
                type="button"
                title={`${task.title} · ${owner?.name ?? '未分配'} · ${task.progress}%`}
                onClick={() => onOpenTask(task.id)}
              >
                <span className="priority-pill" style={priorityStyle(task.pr)}>
                  {task.pr}
                </span>
                <span className="chat-task-chip-title">{task.title}</span>
                <span
                  className="status-pill"
                  style={{ background: status.background, color: status.color }}
                >
                  {task.status}
                </span>
              </button>
            );
          })}
        </div>
      )}

      <div className="messages" ref={messagesRef}>
        {messages.map((message) => (
          <MessageItem
            key={message.id}
            message={message}
            agent={agentById(message.from)}
            onOpenAgent={onOpenAgent}
            agents={agents}
            onConfirmBrief={onConfirmBrief}
            onRejectBrief={onRejectBrief}
            onResolveCardApproval={onResolveCardApproval}
            onAnswerCardClarification={onAnswerCardClarification}
          />
        ))}
        {approvals.map(({ approval, task }) => {
          const owner = task.owner ? agentById(task.owner) : undefined;
          return (
            <article className="chat-approval" key={approval.id}>
              <header>
                {materialIcon('approval', 'warning')}
                <span>
                  <strong>待你拍板 · {approval.title}</strong>
                  <em>{approval.description}</em>
                </span>
                {approval.riskLevel && (
                  <span className="chat-approval-risk">{approval.riskLevel}</span>
                )}
              </header>
              <footer>
                <button
                  className="chat-approval-link"
                  type="button"
                  onClick={() => onOpenTask(task.id)}
                >
                  {task.title}
                  {owner ? ` · ${owner.name}` : ''}
                </button>
                <div className="chat-approval-actions">
                  <button
                    className="button secondary"
                    type="button"
                    onClick={() => onResolveApproval(approval, 'rejected')}
                  >
                    驳回
                  </button>
                  <button
                    className="button primary"
                    type="button"
                    onClick={() => onResolveApproval(approval, 'approved')}
                  >
                    确认通过
                  </button>
                </div>
              </footer>
            </article>
          );
        })}
        {typingName && (
          <div className="typing-line">
            <i />
            {typingName} 正在输入...
          </div>
        )}
      </div>

      <footer className="composer">
        {mentionOpen && mentionMatches.length > 0 && (
          <div className="mention-popover">
            {mentionMatches.map((agent, index) => (
              <button
                className={mentionIndex === index ? 'active' : ''}
                key={agent.id}
                type="button"
                onMouseDown={(event) => {
                  event.preventDefault();
                  insertMention(agent);
                }}
              >
                <span
                  className="mention-avatar"
                  style={{ background: avatarColor(agent) }}
                >
                  {avatarText(agent.name)}
                </span>
                <span>
                  <strong>{agent.name}</strong>
                  <em>{agent.role}</em>
                </span>
              </button>
            ))}
          </div>
        )}
        <div className="composer-box">
          <input
            ref={inputRef}
            value={draft}
            placeholder={placeholder}
            onChange={(event) => {
              const nextDraft = event.currentTarget.value;
              onDraftChange(nextDraft);
              updateMentionState(
                nextDraft,
                event.currentTarget.selectionStart ?? nextDraft.length,
              );
            }}
            onClick={(event) =>
              updateMentionState(
                draft,
                event.currentTarget.selectionStart ?? draft.length,
              )
            }
            onFocus={(event) =>
              updateMentionState(
                draft,
                event.currentTarget.selectionStart ?? draft.length,
              )
            }
            onKeyDown={(event) => {
              if (mentionOpen && mentionMatches.length > 0) {
                if (event.key === 'ArrowDown') {
                  event.preventDefault();
                  setMentionIndex(
                    (current) => (current + 1) % mentionMatches.length,
                  );
                  return;
                }
                if (event.key === 'ArrowUp') {
                  event.preventDefault();
                  setMentionIndex(
                    (current) =>
                      (current - 1 + mentionMatches.length) %
                      mentionMatches.length,
                  );
                  return;
                }
                if (event.key === 'Enter' || event.key === 'Tab') {
                  event.preventDefault();
                  insertMention(mentionMatches[mentionIndex]);
                  return;
                }
              }
              if (event.key === 'Escape' && mentionOpen) {
                event.preventDefault();
                closeMention();
                return;
              }
              if (
                event.key === 'Enter' &&
                !event.shiftKey &&
                !event.nativeEvent.isComposing &&
                event.keyCode !== 229
              ) {
                event.preventDefault();
                onSend();
              }
            }}
          />
          <button type="button" onClick={onSend}>
            {materialIcon('send')}
          </button>
        </div>
        <p>Enter 发送 · 群聊里可以 @员工 点名</p>
      </footer>
    </div>
  );
}

function ApprovalCard({
  raw,
  onResolve,
  onAnswer,
}: {
  raw: string;
  onResolve?: (
    approvalId: string,
    status: 'approved' | 'rejected',
    approvedCapabilityKey?: string,
    scope?: 'once' | 'always',
  ) => Promise<boolean>;
  onAnswer?: (approvalId: string, answer: string) => Promise<boolean>;
}) {
  let data: {
    approval_id: string;
    category: string;
    title?: string;
    description?: string;
    suggested_capability_key?: string;
    risk_gate?: string;
    preview?: {
      channel_name?: string;
      from?: string;
      to?: string[];
      subject?: string;
      body_preview?: string;
    };
  } | null = null;
  try {
    data = JSON.parse(raw);
  } catch {
    data = null;
  }
  const [answer, setAnswer] = useState('');
  const [capKey, setCapKey] = useState(data?.suggested_capability_key ?? '');
  const [resolved, setResolved] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (!data) return <div className="system-message">{raw}</div>;
  const { approval_id: id, category } = data;

  const meta =
    category === 'clarification'
      ? { icon: 'help', kind: 'clarify', label: '员工请求澄清' }
      : category === 'capability_upgrade'
        ? { icon: 'bolt', kind: 'upgrade', label: '员工申请能力升级' }
        : category === 'business_tool'
          ? { icon: 'send', kind: 'business', label: '对外业务动作待确认' }
        : { icon: 'gpp_maybe', kind: 'risk', label: '高风险动作待确认' };

  const doResolve = async (
    status: 'approved' | 'rejected',
    scope: 'once' | 'always' = 'once',
  ) => {
    if (!onResolve) return;
    setBusy(true);
    const ok = await onResolve(
      id,
      status,
      category === 'capability_upgrade' ? capKey : undefined,
      scope,
    );
    setBusy(false);
    if (ok)
      setResolved(
        status === 'rejected'
          ? '已驳回'
          : category === 'capability_upgrade'
            ? `已批准升级：${capKey}`
            : scope === 'always'
              ? '已永久允许（下次不再询问）'
              : '已批准',
      );
  };

  const doAnswer = async () => {
    if (!onAnswer || !answer.trim()) return;
    setBusy(true);
    const ok = await onAnswer(id, answer);
    setBusy(false);
    if (ok) setResolved(`已回复：${answer.trim()}`);
  };

  return (
    <article className={`approval-card approval-${meta.kind}`}>
      <header>
        {materialIcon(meta.icon)}
        <strong>{data.title || meta.label}</strong>
      </header>
      {data.description && <p className="approval-desc">{data.description}</p>}
      {category === 'business_tool' && data.preview && (
        <dl className="approval-business-preview">
          {data.preview.from && <><dt>发件人</dt><dd>{data.preview.from}</dd></>}
          {data.preview.to?.length && <><dt>收件人</dt><dd>{data.preview.to.join('、')}</dd></>}
          {data.preview.subject && <><dt>主题</dt><dd>{data.preview.subject}</dd></>}
          {data.preview.body_preview && <><dt>正文摘要</dt><dd>{data.preview.body_preview}</dd></>}
        </dl>
      )}

      {resolved ? (
        <p className="approval-resolved">
          {materialIcon('check_circle')}
          {resolved}
        </p>
      ) : category === 'clarification' ? (
        <div className="approval-answer">
          <textarea
            rows={2}
            placeholder="回复员工的问题…"
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
          />
          <button
            type="button"
            className="button primary"
            disabled={busy || !answer.trim()}
            onClick={doAnswer}
          >
            提交回复
          </button>
        </div>
      ) : (
        <footer className="approval-actions">
          {category === 'capability_upgrade' && (
            <input
              className="approval-capkey"
              value={capKey}
              placeholder="能力 key（可修改）"
              onChange={(e) => setCapKey(e.target.value)}
            />
          )}
          <button
            type="button"
            className="button primary"
            disabled={busy || (category === 'capability_upgrade' && !capKey.trim())}
            onClick={() => doResolve('approved', 'once')}
          >
            {category === 'capability_upgrade' ? '批准并升级' : '允许一次'}
          </button>
          {category === 'business_tool' && data.risk_gate !== 'prohibited_auto' && (
            <button
              type="button"
              className="button secondary"
              disabled={busy}
              onClick={() => doResolve('approved', 'always')}
              title="以后同类动作不再询问"
            >
              永远允许
            </button>
          )}
          <button
            type="button"
            className="button secondary"
            disabled={busy}
            onClick={() => doResolve('rejected')}
          >
            拒绝
          </button>
        </footer>
      )}
    </article>
  );
}

function MessageItem({
  message,
  agent,
  agents,
  onOpenAgent,
  onConfirmBrief,
  onRejectBrief,
  onResolveCardApproval,
  onAnswerCardClarification,
}: {
  message: Message;
  agent?: Agent;
  agents: Agent[];
  onOpenAgent: (id: string) => void;
  onConfirmBrief?: (briefId: string) => Promise<boolean>;
  onRejectBrief?: (briefId: string) => Promise<boolean>;
  onResolveCardApproval?: (
    approvalId: string,
    status: 'approved' | 'rejected',
    approvedCapabilityKey?: string,
    scope?: 'once' | 'always',
  ) => Promise<boolean>;
  onAnswerCardClarification?: (
    approvalId: string,
    answer: string,
  ) => Promise<boolean>;
}) {
  // TD-06-T3: approval / clarification / capability-upgrade cards in chat
  if (message.type === 'system' && message.text.startsWith('APPROVAL_CARD:')) {
    return (
      <ApprovalCard
        raw={message.text.slice('APPROVAL_CARD:'.length)}
        onResolve={onResolveCardApproval}
        onAnswer={onAnswerCardClarification}
      />
    );
  }

  // Check for BRIEF_CARD message type
  if (message.type === 'system' && message.text.startsWith('BRIEF_CARD:')) {
    const briefJson = message.text.slice('BRIEF_CARD:'.length);
    let briefData: {
      id: string;
      status: string;
      goal: string;
      scope?: string;
      constraints?: string;
      success_criteria?: string;
      owner_agent_id?: string;
      work_items?: Array<{
        key: string;
        title: string;
        owner_agent_id: string;
        expected_output: string;
        depends_on_keys: string[];
        final_delivery: boolean;
      }>;
    };
    try {
      briefData = JSON.parse(briefJson);
    } catch {
      return <div className="system-message">{message.text}</div>;
    }

    const ownerAgent = briefData.owner_agent_id
      ? agents.find((a) => a.id === briefData.owner_agent_id)
      : undefined;

    const handleConfirm = async () => {
      if (!onConfirmBrief) return;
      await onConfirmBrief(briefData.id);
    };

    const handleReject = async () => {
      if (!onRejectBrief) return;
      await onRejectBrief(briefData.id);
    };

    return (
      <article className="brief-card">
        <header>
          <span className="brief-card-icon">📋</span>
          <strong>共识纪要（待确认）</strong>
        </header>
        <div className="brief-card-body">
          <section>
            <strong>目标：</strong>
            <p>{briefData.goal}</p>
          </section>
          {briefData.scope && (
            <section>
              <strong>范围：</strong>
              <p>{briefData.scope}</p>
            </section>
          )}
          {briefData.constraints && (
            <section>
              <strong>约束：</strong>
              <p>{briefData.constraints}</p>
            </section>
          )}
          {briefData.success_criteria && (
            <section>
              <strong>成功标准：</strong>
              <p>{briefData.success_criteria}</p>
            </section>
          )}
          {ownerAgent && (
            <section>
              <strong>负责人：</strong>
              <button
                type="button"
                className="brief-card-owner"
                onClick={() => onOpenAgent(ownerAgent.id)}
              >
                {ownerAgent.name}
              </button>
            </section>
          )}
          {briefData.work_items && briefData.work_items.length > 0 && (
            <section className="brief-work-items">
              <strong>分工与接力：</strong>
              <ol>
                {briefData.work_items.map((item) => {
                  const owner = agents.find((agent) => agent.id === item.owner_agent_id);
                  const nextOwners = briefData.work_items
                    ?.filter((candidate) => candidate.depends_on_keys.includes(item.key))
                    .map(
                      (candidate) =>
                        agents.find((agent) => agent.id === candidate.owner_agent_id)?.name ??
                        candidate.title,
                    );
                  return (
                    <li key={item.key}>
                      <span>
                        <b>{owner?.name ?? '待定员工'}</b>负责{item.title}，交付
                        {item.expected_output}
                        {nextOwners && nextOwners.length > 0
                          ? `，完成后交给${nextOwners.join('、')}`
                          : '，完成后汇总交付'}
                      </span>
                    </li>
                  );
                })}
              </ol>
            </section>
          )}
        </div>
        <footer className="brief-card-footer">
          <button
            type="button"
            className="button primary green"
            onClick={handleConfirm}
          >
            确认并启动
          </button>
          <button
            type="button"
            className="button secondary"
            onClick={handleReject}
          >
            不对，继续讨论
          </button>
        </footer>
      </article>
    );
  }

  if (message.type === 'system') {
    return <div className="system-message">{message.text}</div>;
  }

  const isBoss = message.from === 'boss';
  const avatarBg = isBoss ? '#2C313A' : agent ? avatarColor(agent) : '#9AA1AD';

  return (
    <div className="message-row">
      <button
        className="message-avatar"
        style={{ background: avatarBg }}
        type="button"
        onClick={() => agent && onOpenAgent(agent.id)}
      >
        {isBoss ? '我' : agent ? avatarText(agent.name) : ''}
      </button>
      <div className="message-body">
        <div className="message-meta">
          <strong>{isBoss ? '我（老板）' : agent?.name}</strong>
          {!isBoss && <span>{agent?.role}</span>}
          <em>{message.time}</em>
          {message.provider && (
            <em className="model-badge">
              {message.provider}
              {message.model ? ` · ${message.model}` : ''}
            </em>
          )}
        </div>
        <p className="message-text">
          {renderMentionText(message.text, agents)}
        </p>
      </div>
    </div>
  );
}

function StaffView({
  companyName,
  departments,
  agents,
  tasks,
  busyCount,
  onOpenCreate,
  onOpenTeamCompiler,
  onOpenAgent,
}: {
  companyName: string;
  departments: Department[];
  agents: Agent[];
  tasks: Task[];
  busyCount: number;
  onOpenCreate: () => void;
  onOpenTeamCompiler: () => void;
  onOpenAgent: (id: string) => void;
}) {
  const { t } = useTranslation();
  // Breadcrumb path of department ids, root → current. Multi-level org chart
  // (technical dept → backend center → data group), not a flat list — a
  // department can nest under another via parent_id.
  const [path, setPath] = useState<string[]>([]);

  const deptById = new Map(departments.map((dept) => [dept.id, dept]));
  const childrenOf = (parentId: string | null) =>
    departments
      .filter((dept) => (dept.parent_id ?? null) === parentId)
      .sort((a, b) => a.sort_order - b.sort_order);

  const descendantMembers = (deptId: string): Agent[] => {
    const direct = agents.filter((agent) => agent.departmentId === deptId);
    const subMembers = childrenOf(deptId).flatMap((child) =>
      descendantMembers(child.id),
    );
    return [...direct, ...subMembers];
  };

  const currentParentId = path.length ? path[path.length - 1] : null;
  const currentDept = currentParentId ? deptById.get(currentParentId) : null;
  const childDepts = childrenOf(currentParentId);
  const directMembers = currentParentId
    ? agents.filter((agent) => agent.departmentId === currentParentId)
    : [];

  const renderMemberRow = (agent: Agent) => {
    const currentTask = tasks.find(
      (task) => task.owner === agent.id && task.status !== '已完成',
    );
    return (
      <button
        className="org-member-row"
        key={agent.id}
        type="button"
        onClick={() => onOpenAgent(agent.id)}
      >
        <div
          className="org-member-avatar"
          style={{ background: avatarColor(agent) }}
        >
          {avatarText(agent.name)}
        </div>
        <div className="org-member-copy">
          <strong>
            {agent.name}
            {agent.role === '老板秘书' && <em>{t('staff.builtinSecretary')}</em>}
          </strong>
          <p>
            {agent.role}
            {currentTask ? ` | ${currentTask.title}` : ''}
          </p>
        </div>
        <span style={{ color: dotColor(agent.statusKind) }}>
          <i style={{ background: dotColor(agent.statusKind) }} />
          {agent.statusLabel}
        </span>
      </button>
    );
  };

  return (
    <div className="screen-scroll">
      <div className="screen-inner">
        <section className="org-directory">
          <header className="org-header">
            <div>
              <h1>{t('staff.title')}</h1>
              <p>
                {t('staff.summary', {
                  company: companyName,
                  agentCount: agents.length,
                  deptCount: departments.length,
                  busyCount,
                })}
              </p>
            </div>
            <div className="org-header-actions">
              <button
                className="button secondary"
                type="button"
                onClick={onOpenTeamCompiler}
              >
                {materialIcon('auto_awesome')}{t('staff.compileTeam')}
              </button>
              <button
                className="button secondary blue"
                type="button"
                onClick={onOpenCreate}
              >
                {materialIcon('add_circle')}{t('staff.createEmployee')}
              </button>
            </div>
          </header>

          <div className="org-body">
            <nav className="org-breadcrumb" aria-label="组织路径">
              <button type="button" onClick={() => setPath([])}>
                {companyName}
              </button>
              {path.map((deptId, index) => (
                <Fragment key={deptId}>
                  {materialIcon('chevron_right')}
                  <button
                    type="button"
                    onClick={() => setPath(path.slice(0, index + 1))}
                  >
                    {deptById.get(deptId)?.name ?? deptId}
                  </button>
                </Fragment>
              ))}
            </nav>

            <div className="org-list" aria-label="部门与成员">
              {childDepts.map((dept) => {
                const members = descendantMembers(dept.id);
                const busyMembers = members.filter(
                  (agent) => agent.statusKind === 'busy',
                ).length;
                const waitingMembers = members.filter(
                  (agent) => agent.statusKind === 'wait',
                ).length;

                return (
                  <div className="org-row" key={dept.id}>
                    <div className="org-node-icon">
                      {materialIcon('account_tree')}
                    </div>
                    <div className="org-row-main">
                      <strong>
                        {dept.name}
                        <span>({members.length})</span>
                      </strong>
                      <p>
                        {t('staff.busyMembers', { count: busyMembers })} ·{' '}
                        {t('staff.waitingMembers', { count: waitingMembers })}
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={() => setPath([...path, dept.id])}
                    >
                      {t('staff.subLevel')}
                    </button>
                  </div>
                );
              })}

              {currentParentId && directMembers.map(renderMemberRow)}

              {!childDepts.length && !directMembers.length && (
                <EmptyState>
                  {currentDept
                    ? t('staff.noSubDeptOrMember', { name: currentDept.name })
                    : t('staff.noDept')}
                </EmptyState>
              )}
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}

function TalentMarketView({
  agents,
  templates,
  categories,
  skillCount,
  mcpCount,
  onRecruit,
}: {
  agents: Agent[];
  templates: HireTemplate[];
  categories: TalentCategory[];
  skillCount: number;
  mcpCount: number;
  onRecruit: (templateId: string) => void;
}) {
  const { t } = useTranslation();
  const [activeCategory, setActiveCategory] = useState('全部');
  const [keyword, setKeyword] = useState('');
  const [detailTemplateId, setDetailTemplateId] = useState<string | null>(null);
  const categoryOptions = [
    {
      id: '全部',
      name: t('market.all'),
      description: t('market.allDescription'),
    },
    ...categories,
  ];
  const visibleTemplates = templates.filter((template) => {
    const matchCategory =
      activeCategory === '全部' || template.categoryId === activeCategory;
    const query = keyword.trim().toLowerCase();
    if (!query) return matchCategory;
    return (
      matchCategory &&
      [
        template.name,
        template.category,
        template.dept,
        template.desc,
        template.prompt,
        ...template.skills,
        ...template.mcps,
      ]
        .join(' ')
        .toLowerCase()
        .includes(query)
    );
  });
  const detailTemplate = detailTemplateId
    ? templates.find((template) => template.id === detailTemplateId)
    : null;
  const activeCategoryName =
    categoryOptions.find((category) => category.id === activeCategory)?.name ??
    t('market.all');

  return (
    <>
      <div className="market-screen">
        <header className="page-header compact">
          <div>
            <h1>{t('market.title')}</h1>
            <p>{t('market.subtitle')}</p>
          </div>
        </header>

        <section className="market-summary" aria-label={t('market.overviewAria')}>
          <div>
            {materialIcon('badge')}
            <span>
              <strong>{templates.length}</strong>
              <em>{t('market.officialTalent')}</em>
            </span>
          </div>
          <div>
            {materialIcon('verified_user')}
            <span>
              <strong>{skillCount}</strong>
              <em>{t('market.bindableSkills')}</em>
            </span>
          </div>
          <div>
            {materialIcon('extension')}
            <span>
              <strong>{mcpCount}</strong>
              <em>{t('market.mcpTools')}</em>
            </span>
          </div>
          <div>
            {materialIcon('group')}
            <span>
              <strong>{agents.length}</strong>
              <em>{t('market.hiredEmployees')}</em>
            </span>
          </div>
        </section>

        <section className="market-layout">
          <aside className="market-filter" aria-label={t('market.filterAria')}>
            <strong>{t('market.officialCategories')}</strong>
            <p>{t('market.categoriesNote')}</p>
            <div>
              {categoryOptions.map((category) => {
                const count =
                  category.id === '全部'
                    ? templates.length
                    : templates.filter(
                        (template) => template.categoryId === category.id,
                      ).length;
                return (
                  <button
                    className={activeCategory === category.id ? 'active' : ''}
                    key={category.id}
                    type="button"
                    title={category.description}
                    onClick={() => setActiveCategory(category.id)}
                  >
                    <span>{category.name}</span>
                    <em>{count}</em>
                  </button>
                );
              })}
            </div>
            <footer>{t('market.footerNote')}</footer>
          </aside>

          <div className="market-main">
            <div className="market-toolbar">
              <label>
                {materialIcon('search')}
                <input
                  value={keyword}
                  placeholder={t('market.searchPlaceholder')}
                  onChange={(event) => setKeyword(event.target.value)}
                />
              </label>
              <span>
                {t('market.categoryCount', {
                  category: activeCategoryName,
                  count: visibleTemplates.length,
                })}
              </span>
            </div>

            <div className="market-list">
              {visibleTemplates.map((template, index) => (
                <button
                  className="market-card"
                  key={template.id}
                  type="button"
                  onClick={() => setDetailTemplateId(template.id)}
                >
                  <div className="market-card-main">
                    <div
                      className="market-role-icon"
                      style={{
                        background: `oklch(0.55 0.11 ${220 + index * 30})`,
                      }}
                    >
                      {['◆', '●', '▲', '■', '◗', '✱'][index % 6]}
                    </div>
                    <div className="market-copy">
                      <div>
                        <strong>{template.name}</strong>
                        <span>{t('market.officiallyPublished')}</span>
                      </div>
                      <p>
                        {t('market.cardMeta', {
                          category: template.category,
                          dept: template.dept,
                          desc: template.desc,
                        })}
                      </p>
                      <em>{template.prompt}</em>
                      <div className="market-tags">
                        {template.skills.map((skill) => (
                          <span key={skill}>
                            {materialIcon('bolt')}
                            {skill}
                          </span>
                        ))}
                        {template.mcps.map((mcp) => (
                          <span className="muted" key={mcp}>
                            {materialIcon('extension')}
                            {mcp}
                          </span>
                        ))}
                      </div>
                    </div>
                  </div>
                  <div className="market-card-actions">
                    <span>{template.version}</span>
                    <small>{materialIcon('badge')}{t('market.talentProfile')}</small>
                  </div>
                </button>
              ))}
              {visibleTemplates.length === 0 && (
                <div className="market-empty">{t('market.noMatch')}</div>
              )}
            </div>
          </div>
        </section>
      </div>
      {detailTemplate && (
        <TalentDetailModal
          template={detailTemplate}
          onClose={() => setDetailTemplateId(null)}
          onRecruit={() => {
            setDetailTemplateId(null);
            onRecruit(detailTemplate.id);
          }}
        />
      )}
    </>
  );
}

function TalentDetailModal({
  template,
  onClose,
  onRecruit,
}: {
  template: HireTemplate;
  onClose: () => void;
  onRecruit: () => void;
}) {
  const { t } = useTranslation();
  return (
    <Modal
      title={template.name}
      description={t('market.detailDescription', {
        category: template.category,
        version: template.version,
      })}
      width={760}
      onClose={onClose}
    >
      <div className="talent-detail-head">
        <div
          className="large-avatar"
          style={{ background: 'oklch(0.55 0.11 230)' }}
        >
          {avatarText(template.name)}
        </div>
        <div>
          <strong>{template.name}</strong>
          <p>{template.desc}</p>
          <span>
            {template.publisher} · {template.status === 'published' ? t('market.published') : template.status}
          </span>
        </div>
      </div>

      <FieldLabel>{t('market.basicInfo')}</FieldLabel>
      <div className="talent-detail-grid">
        <div>
          <span>{t('market.officialCategory')}</span>
          <strong>{template.category}</strong>
        </div>
        <div>
          <span>{t('market.suggestedDept')}</span>
          <strong>{template.dept}</strong>
        </div>
        <div>
          <span>{t('market.version')}</span>
          <strong>{template.version}</strong>
        </div>
        <div>
          <span>{t('market.templateId')}</span>
          <strong>{template.id}</strong>
        </div>
        <div>
          <span>{t('market.templateSource')}</span>
          <strong>{template.publisher}</strong>
        </div>
        <div>
          <span>{t('market.categoryId')}</span>
          <strong>{template.categoryId}</strong>
        </div>
      </div>

      <FieldLabel>{t('market.talentDescription')}</FieldLabel>
      <div className="talent-profile-note">{template.desc}</div>

      <FieldLabel>{t('market.rolePrompt')}</FieldLabel>
      <div className="prompt-box">{template.prompt}</div>

      <FieldLabel>Skills</FieldLabel>
      <ChipList items={template.skills} emptyText={t('market.noSkills')} />

      <FieldLabel>{t('market.mcpTools')}</FieldLabel>
      <ChipList items={template.mcps} emptyText={t('market.noMcp')} />

      <FieldLabel>{t('market.platformNote')}</FieldLabel>
      <div className="market-admin-note">{t('market.platformNoteBody')}</div>

      <div className="modal-actions">
        <button className="button secondary" type="button" onClick={onClose}>
          {t('common.close')}
        </button>
        <button className="button primary" type="button" onClick={onRecruit}>
          {materialIcon('person_add')}{t('market.recruit')}
        </button>
      </div>
    </Modal>
  );
}

function TasksView({
  tasks,
  tabs,
  activeFilter,
  agents,
  scopeLabel,
  onPickFilter,
  onClearScope,
  onOpenCreateTask,
  onAdvanceTask,
  onOpenClaimTask,
  onOpenTask,
  onOpenChat,
  onOpenAgent,
}: {
  tasks: Task[];
  tabs: Array<{ status: TaskStatus | '全部'; count: number }>;
  activeFilter: TaskStatus | '全部';
  agents: Agent[];
  scopeLabel: string | null;
  onPickFilter: (status: TaskStatus | '全部') => void;
  onClearScope: () => void;
  onOpenCreateTask: () => void;
  onAdvanceTask: (task: Task) => void;
  onOpenClaimTask: (taskId: string) => void;
  onOpenTask: (taskId: string) => void;
  onOpenChat: (id: string) => void;
  onOpenAgent: (id: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="screen-scroll">
      <div className="screen-inner">
        <header className="page-header compact">
          <div>
            <h1>{t('tasks.title')}</h1>
            <p>{t('tasks.subtitle')}</p>
          </div>
          <div className="header-actions">
            <button
              className="button primary"
              type="button"
              onClick={onOpenCreateTask}
            >
              {materialIcon('add_task')}{t('tasks.create')}
            </button>
          </div>
        </header>

        {scopeLabel && (
          <div className="task-scope-bar">
            <span>
              {materialIcon('forum')}{t('tasks.viewingScope', { scope: scopeLabel })}
            </span>
            <button type="button" onClick={onClearScope}>
              {t('tasks.viewAll')}
            </button>
          </div>
        )}

        <div className="task-filter-bar" aria-label={t('tasks.filterAria')}>
          {tabs.map((tab) => (
            <button
              className={
                activeFilter === tab.status
                  ? 'task-filter-button active'
                  : 'task-filter-button'
              }
              key={tab.status}
              type="button"
              onClick={() => onPickFilter(tab.status)}
            >
              <span>{tab.status}</span>
              <em>{tab.count}</em>
            </button>
          ))}
        </div>

        <article className="card task-table">
          <div className="task-table-head">
            <span>{t('tasks.colPriority')}</span>
            <span>{t('tasks.colTask')}</span>
            <span>{t('tasks.colOwner')}</span>
            <span>{t('tasks.colProgress')}</span>
            <span>{t('tasks.colStatus')}</span>
            <span>{t('tasks.colActions')}</span>
          </div>
          {tasks.length === 0 && (
            <div className="task-table-empty">
              {scopeLabel ? t('tasks.emptyScoped') : t('tasks.emptyGlobal')}
            </div>
          )}
          {tasks.map((task) => {
            const owner = agents.find((agent) => agent.id === task.owner);
            const suggestedAgent = task.suggestedAgentId
              ? agents.find((agent) => agent.id === task.suggestedAgentId)
              : null;
            const priority = priorityStyle(task.pr);
            const status = statusStyle(task.status);
            return (
              <div className="task-table-row" key={task.id}>
                <span className="priority-pill" style={priority}>
                  {task.pr}
                </span>
                <div className="task-title-cell">
                  <strong>{task.title}</strong>
                  {task.src && (
                    <button type="button" onClick={() => onOpenChat(task.src)}>
                      {task.srcLabel}
                    </button>
                  )}
                </div>
                <div className="owner-cell">
                  {owner && (
                    <button
                      className="tiny-avatar"
                      style={{ background: avatarColor(owner) }}
                      type="button"
                      onClick={() => onOpenAgent(owner.id)}
                    >
                      {avatarText(owner.name)}
                    </button>
                  )}
                  <span>{owner?.name ?? t('tasks.unassigned')}</span>
                  {!owner && suggestedAgent && (
                    <em className="owner-suggestion">
                      {t('tasks.suggested', { name: suggestedAgent.name })}
                    </em>
                  )}
                </div>
                <div className="task-progress">
                  <div className="progress-track">
                    <i
                      style={{
                        width: `${task.progress}%`,
                        background: status.bar,
                      }}
                    />
                  </div>
                  <em>{task.progress}%</em>
                </div>
                <span
                  className="status-pill"
                  style={{ background: status.background, color: status.color }}
                >
                  {task.status}
                </span>
                <div className="task-actions">
                  <button type="button" onClick={() => onOpenTask(task.id)}>
                    {t('tasks.detail')}
                  </button>
                  {task.taskPlanId ? (
                    <span className="task-auto-label">
                      {materialIcon('autoplay')}自动执行
                    </span>
                  ) : task.status === '待认领' ? (
                    <button
                      type="button"
                      onClick={() => onOpenClaimTask(task.id)}
                    >
                      {t('tasks.claim')}
                    </button>
                  ) : (
                    <button type="button" onClick={() => onAdvanceTask(task)}>
                      {task.status === '已完成' ? task.status : t('tasks.advance')}
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </article>
      </div>
    </div>
  );
}

function LibraryView({
  tabs,
  activeTab,
  onPickTab,
  knowledgeSources,
  skills,
  mcps,
  onOpenKnowledge,
  onSyncObsidian,
  obsidianSyncing,
}: {
  tabs: Array<{ key: LibraryTab; label: string }>;
  activeTab: LibraryTab;
  onPickTab: (tab: LibraryTab) => void;
  knowledgeSources: KnowledgeSource[];
  skills: string[];
  mcps: string[];
  onOpenKnowledge: () => void;
  onSyncObsidian: () => void;
  obsidianSyncing: boolean;
}) {
  const { t } = useTranslation();
  const categoryCount = new Set(knowledgeSources.map((source) => source.category))
    .size;

  return (
    <div className="screen-scroll">
      <div className="screen-inner">
        <header className="page-header compact">
          <div>
            <h1>{t('library.title')}</h1>
            <p>{t('library.subtitle')}</p>
          </div>
          {activeTab === 'docs' && (
            <div className="page-header-actions">
              <button className="button secondary" type="button" onClick={onSyncObsidian} disabled={obsidianSyncing}>
                {materialIcon('sync')}{obsidianSyncing ? t('library.syncingObsidian') : t('library.syncObsidian')}
              </button>
              <button className="button primary" type="button" onClick={onOpenKnowledge}>
                {materialIcon('note_add')}{t('library.addSource')}
              </button>
            </div>
          )}
        </header>

        <div className="tabs">
          {tabs.map((tab) => (
            <button
              className={activeTab === tab.key ? 'tab active' : 'tab'}
              key={tab.key}
              type="button"
              onClick={() => onPickTab(tab.key)}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {activeTab === 'docs' && (
          <>
            <section className="library-summary" aria-label={t('library.overviewAria')}>
              <div>
                {materialIcon('folder_copy')}
                <span>
                  <strong>{knowledgeSources.length}</strong>
                  <em>{t('library.entries')}</em>
                </span>
              </div>
              <div>
                {materialIcon('category')}
                <span>
                  <strong>{categoryCount}</strong>
                  <em>{t('library.categories')}</em>
                </span>
              </div>
              <div>
                {materialIcon('psychology')}
                <span>
                  <strong>LLM</strong>
                  <em>{t('library.autoInjected')}</em>
                </span>
              </div>
            </section>

            <div className="docs-grid">
              {knowledgeSources.map((source) => (
                <article className="doc-card" key={source.id}>
                  <div>{materialIcon('description')}</div>
                  <section>
                    <span>{source.category}</span>
                    <strong>{source.title}</strong>
                    <p>{excerpt(source.content, 130)}</p>
                    {source.origin === 'obsidian' && source.sourceRef && (
                      <em>{materialIcon('folder_open')}{source.sourceRef}</em>
                    )}
                    <em>
                      {materialIcon('schedule')}
                      {t('library.updatedAt', { date: source.updatedAt })}
                    </em>
                  </section>
                </article>
              ))}
              <button
                className="upload-card"
                type="button"
                onClick={onOpenKnowledge}
              >
                {materialIcon('note_add')}{t('library.addCompanySource')}
              </button>
            </div>
            {!knowledgeSources.length && (
              <div className="knowledge-empty">
                <strong>{t('library.emptyTitle')}</strong>
                <p>{t('library.emptyBody')}</p>
              </div>
            )}
          </>
        )}

        {activeTab === 'skills' && (
          <article className="card simple-list">
            {skills.map((skill) => (
              <div className="library-row" key={skill}>
                <div className="library-icon">{materialIcon('bolt')}</div>
                <div>
                  <strong>{skill}</strong>
                  <p>{t('library.skillSource')}</p>
                </div>
              </div>
            ))}
            {!skills.length && <EmptyState>{t('library.noSkills')}</EmptyState>}
          </article>
        )}

        {activeTab === 'mcp' && (
          <article className="card simple-list">
            {mcps.map((mcp) => (
              <div className="library-row" key={mcp}>
                <div className="library-icon neutral">
                  {materialIcon('extension')}
                </div>
                <div>
                  <strong>{mcp}</strong>
                  <p>{t('library.mcpSource')}</p>
                </div>
                <span>
                  <i />
                  {t('library.notConnected')}
                </span>
              </div>
            ))}
            {!mcps.length && <EmptyState>{t('library.noMcp')}</EmptyState>}
          </article>
        )}
      </div>
    </div>
  );
}

function AgentDetail({
  agent,
  tasks,
  token,
  onClose,
  onDm,
}: {
  agent: Agent;
  tasks: Task[];
  token: string;
  onClose: () => void;
  onDm: () => void;
}) {
  const { t } = useTranslation();
  const [learned, setLearned] = useState<
    { name: string; content: string }[] | null
  >(null);
  const [caps, setCaps] = useState<
    {
      capability_key: string;
      status: string;
      required_credentials: string[];
      credential_status: Record<string, boolean>;
    }[] | null
  >(null);
  const [credentialValues, setCredentialValues] = useState<Record<string, string>>({});
  const [credentialBusy, setCredentialBusy] = useState('');
  const [credentialMsg, setCredentialMsg] = useState('');
  const [businessActions, setBusinessActions] = useState<BusinessAction[]>([]);
  const [memories, setMemories] = useState<AgentMemory[]>([]);
  const [companyEvents, setCompanyEvents] = useState<CompanyEventAudit[]>([]);
  const [relationships, setRelationships] = useState<
    Array<{ colleague_name: string; colleague_role: string; summary: string; trust_score: number }>
  >([]);
  const [businessPolicies, setBusinessPolicies] = useState<
    Array<{ id: string; tool_name: string; created_at: string }>
  >([]);
  const [reflecting, setReflecting] = useState(false);
  const [reflectMsg, setReflectMsg] = useState('');
  const [catalog, setCatalog] = useState<
    { key: string; description: string }[] | null
  >(null);
  const [showGrantPicker, setShowGrantPicker] = useState(false);
  const [grantKey, setGrantKey] = useState('');
  const [granting, setGranting] = useState(false);
  const [grantMsg, setGrantMsg] = useState('');

  const loadGrowth = () => {
    apiRequest<{ skills: { name: string; content: string }[] }>(
      `/agents/${agent.id}/skills`,
      { token },
    )
      .then((r) => setLearned(r.skills))
      .catch(() => setLearned([]));
    apiRequest<{
      capabilities: Array<{
        capability_key: string;
        status: string;
        required_credentials: string[];
        credential_status: Record<string, boolean>;
      }>;
    }>(
      `/agents/${agent.id}/spec`,
      { token },
    )
      .then((r) => setCaps(r.capabilities ?? []))
      .catch(() => setCaps([]));
    apiRequest<BusinessAction[]>(`/business-actions?agent_id=${agent.id}`, { token })
      .then(setBusinessActions)
      .catch(() => setBusinessActions([]));
    apiRequest<Array<{ id: string; tool_name: string; created_at: string }>>(
      `/agents/${agent.id}/business-tool-policies`,
      { token },
    )
      .then(setBusinessPolicies)
      .catch(() => setBusinessPolicies([]));
    apiRequest<AgentMemory[]>(`/agents/${agent.id}/memory?limit=12`, { token })
      .then(setMemories)
      .catch(() => setMemories([]));
    apiRequest<typeof relationships>(`/agents/${agent.id}/relationships`, { token })
      .then(setRelationships)
      .catch(() => setRelationships([]));
    apiRequest<CompanyEventAudit[]>('/company-events?limit=200', { token })
      .then(setCompanyEvents)
      .catch(() => setCompanyEvents([]));
  };

  useEffect(() => {
    loadGrowth();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agent.id, token]);

  const triggerReflect = async () => {
    setReflecting(true);
    setReflectMsg('');
    try {
      const r = await apiRequest<{ skills_learned: string[] }>(
        `/agents/${agent.id}/reflect`,
        { method: 'POST', token },
      );
      setReflectMsg(
        r.skills_learned.length
          ? t('agentDetail.newSkillsLearned', { count: r.skills_learned.length })
          : t('agentDetail.nothingToLearn'),
      );
      loadGrowth();
    } catch (err) {
      setReflectMsg(err instanceof Error ? err.message : t('agentDetail.reflectFailed'));
    } finally {
      setReflecting(false);
    }
  };

  const openGrantPicker = () => {
    setGrantMsg('');
    setShowGrantPicker(true);
    if (!catalog) {
      apiRequest<{ key: string; description: string }[]>('/capabilities', { token })
        .then(setCatalog)
        .catch(() => setCatalog([]));
    }
  };

  const grantCapability = async () => {
    if (!grantKey) return;
    setGranting(true);
    setGrantMsg('');
    try {
      await apiRequest(`/agents/${agent.id}/capabilities`, {
        method: 'POST',
        token,
        body: JSON.stringify({ capability_key: grantKey }),
      });
      setGrantMsg(t('agentDetail.grantCapabilitySuccess'));
      setShowGrantPicker(false);
      setGrantKey('');
      loadGrowth();
    } catch (err) {
      setGrantMsg(
        err instanceof Error ? err.message : t('agentDetail.grantCapabilityFailed'),
      );
    } finally {
      setGranting(false);
    }
  };

  const grantableCapabilities = (catalog ?? []).filter(
    (entry) => !(caps ?? []).some((cap) => cap.capability_key === entry.key),
  );
  const credentials = Array.from(
    new Set((caps ?? []).flatMap((cap) => cap.required_credentials ?? [])),
  );

  const saveCredential = async (name: string) => {
    const value = credentialValues[name]?.trim();
    if (!value) return;
    setCredentialBusy(name);
    setCredentialMsg('');
    try {
      await apiRequest(`/agents/${agent.id}/credentials`, {
        method: 'POST',
        token,
        body: JSON.stringify({ credential_name: name, value }),
      });
      setCredentialValues((current) => ({ ...current, [name]: '' }));
      setCredentialMsg(`${name} 已安全保存`);
      loadGrowth();
    } catch (error) {
      setCredentialMsg(error instanceof Error ? error.message : '凭证保存失败');
    } finally {
      setCredentialBusy('');
    }
  };

  const revokeCredential = async (name: string) => {
    setCredentialBusy(name);
    setCredentialMsg('');
    try {
      await apiRequest(`/agents/${agent.id}/credentials/${encodeURIComponent(name)}`, {
        method: 'DELETE',
        token,
      });
      setCredentialMsg(`${name} 已撤销`);
      loadGrowth();
    } catch (error) {
      setCredentialMsg(error instanceof Error ? error.message : '凭证撤销失败');
    } finally {
      setCredentialBusy('');
    }
  };

  const revokeBusinessPolicy = async (toolName: string) => {
    try {
      await apiRequest(
        `/agents/${agent.id}/business-tool-policies/${encodeURIComponent(toolName)}`,
        { method: 'DELETE', token },
      );
      loadGrowth();
    } catch (error) {
      setCredentialMsg(error instanceof Error ? error.message : '撤销长期放行失败');
    }
  };

  const skillTitle = (content: string, fallback: string) => {
    const first = content.split('\n').find((l) => l.trim());
    return first ? first.replace(/^#+\s*/, '').trim() || fallback : fallback;
  };

  const capLabel: Record<string, string> = {
    enabled: t('agentDetail.capEnabled'),
    credential_missing: t('agentDetail.capCredentialMissing'),
    pending: t('agentDetail.capPending'),
    disabled: t('agentDetail.capDisabled'),
  };

  return (
    <>
      <div className="overlay" onClick={onClose} />
      <aside className="agent-drawer" aria-label={t('agentDetail.aria', { name: agent.name })}>
        <header>
          <div className="drawer-topline">
            <div
              className="large-avatar"
              style={{ background: avatarColor(agent) }}
            >
              {avatarText(agent.name)}
            </div>
            <button type="button" title={t('common.close')} onClick={onClose}>
              {materialIcon('close')}
            </button>
          </div>
          <h2>
            {agent.name}
            <span>
              <i style={{ background: dotColor(agent.statusKind) }} />
              {agent.statusLabel}
            </span>
          </h2>
          <p>
            {agent.role} · {agent.dept} · {agent.joined}
          </p>
          <div className="drawer-actions">
            <button className="button primary" type="button" onClick={onDm}>
              {materialIcon('chat')}{t('agentDetail.dm')}
            </button>
          </div>
        </header>

        <div className="drawer-scroll">
          <section className="drawer-section">
            <h3>{t('agentDetail.description')}</h3>
            <p className="prompt-box">{agent.description || t('agentDetail.noDescription')}</p>
          </section>

          <section className="drawer-section">
            <h3>{t('market.rolePrompt')}</h3>
            <p className="prompt-box">{agent.prompt}</p>
          </section>

          <section className="drawer-section">
            <h3>Skills</h3>
            <ChipList items={agent.skills} emptyText={t('agentDetail.noSkillsBound')} />
          </section>

          <section className="drawer-section">
            <h3>MCP</h3>
            <ChipList items={agent.mcps} emptyText={t('market.noMcp')} muted />
          </section>

          <section className="drawer-section">
            <div className="growth-head">
              <h3>{t('agentDetail.growthTrajectory')}</h3>
              <button
                type="button"
                className="button small"
                onClick={triggerReflect}
                disabled={reflecting}
              >
                {materialIcon('auto_awesome')}
                {reflecting ? t('agentDetail.reflecting') : t('agentDetail.triggerReflect')}
              </button>
            </div>
            {reflectMsg && <p className="growth-msg">{reflectMsg}</p>}

            <div className="growth-head">
              <h4 className="growth-sub">{t('agentDetail.capabilitiesGained')}</h4>
              <button
                type="button"
                className="button small"
                onClick={openGrantPicker}
              >
                {t('agentDetail.grantCapability')}
              </button>
            </div>
            {grantMsg && <p className="growth-msg">{grantMsg}</p>}
            {showGrantPicker && (
              <div className="grant-capability-picker">
                {catalog === null ? null : grantableCapabilities.length === 0 ? (
                  <EmptyState>{t('agentDetail.grantCapabilityNoneLeft')}</EmptyState>
                ) : (
                  <>
                    <label>
                      {t('agentDetail.grantCapabilityPick')}
                      <select
                        value={grantKey}
                        onChange={(e) => setGrantKey(e.target.value)}
                      >
                        <option value="" disabled>
                          —
                        </option>
                        {grantableCapabilities.map((entry) => (
                          <option key={entry.key} value={entry.key}>
                            {entry.key} — {entry.description}
                          </option>
                        ))}
                      </select>
                    </label>
                    <div className="drawer-actions">
                      <button
                        type="button"
                        className="button primary small"
                        disabled={!grantKey || granting}
                        onClick={grantCapability}
                      >
                        {t('agentDetail.grantCapabilityConfirm')}
                      </button>
                      <button
                        type="button"
                        className="button small"
                        onClick={() => setShowGrantPicker(false)}
                      >
                        {t('agentDetail.grantCapabilityCancel')}
                      </button>
                    </div>
                  </>
                )}
              </div>
            )}
            {caps && caps.length === 0 && (
              <EmptyState>{t('agentDetail.noCapabilities')}</EmptyState>
            )}
            <div className="growth-caps">
              {(caps ?? []).map((cap) => (
                <span
                  className={`cap-badge cap-${cap.status}`}
                  key={cap.capability_key}
                >
                  {cap.capability_key}
                  <em>{capLabel[cap.status] ?? cap.status}</em>
                </span>
              ))}
            </div>

            {credentials.length > 0 && (
              <>
                <h4 className="growth-sub">业务凭证</h4>
                {credentialMsg && <p className="growth-msg">{credentialMsg}</p>}
                <div className="credential-list">
                  {credentials.map((name) => {
                    const configured = (caps ?? []).some(
                      (cap) => cap.credential_status?.[name],
                    );
                    return (
                      <div className="credential-row" key={name}>
                        <div>
                          {materialIcon(configured ? 'verified_user' : 'key')}
                          <span><strong>{name}</strong><em>{configured ? '已配置' : '待配置'}</em></span>
                        </div>
                        <input
                          type="password"
                          value={credentialValues[name] ?? ''}
                          placeholder={configured ? '输入新值以替换' : '输入凭证'}
                          onChange={(event) =>
                            setCredentialValues((current) => ({
                              ...current,
                              [name]: event.target.value,
                            }))
                          }
                        />
                        <button
                          className="button primary small"
                          type="button"
                          disabled={credentialBusy === name || !credentialValues[name]?.trim()}
                          onClick={() => saveCredential(name)}
                        >
                          {materialIcon('save')}保存
                        </button>
                        {configured && (
                          <button
                            className="icon-button"
                            type="button"
                            title="撤销凭证"
                            disabled={credentialBusy === name}
                            onClick={() => revokeCredential(name)}
                          >
                            {materialIcon('delete')}
                          </button>
                        )}
                      </div>
                    );
                  })}
                </div>
              </>
            )}

            <h4 className="growth-sub">长期放行</h4>
            {businessPolicies.length === 0 ? (
              <EmptyState>暂无长期放行</EmptyState>
            ) : (
              <div className="business-policy-list">
                {businessPolicies.map((policy) => (
                  <div key={policy.id}>
                    <span>{materialIcon('verified')}<strong>{policy.tool_name}</strong></span>
                    <button
                      className="icon-button"
                      type="button"
                      title="撤销长期放行"
                      onClick={() => revokeBusinessPolicy(policy.tool_name)}
                    >
                      {materialIcon('close')}
                    </button>
                  </div>
                ))}
              </div>
            )}

            <h4 className="growth-sub">最近对外动作</h4>
            {businessActions.length === 0 ? (
              <EmptyState>暂无对外动作</EmptyState>
            ) : (
              <div className="business-action-list">
                {businessActions.slice(0, 8).map((action) => (
                  <article key={action.id}>
                    <div>
                      {materialIcon(action.status === 'succeeded' ? 'check_circle' : 'outgoing_mail')}
                      <span>
                        <strong>{action.preview.subject || action.tool_name}</strong>
                        <em>{action.preview.to?.join('、') || action.tool_name}</em>
                      </span>
                    </div>
                    <span className={`run-trace-status run-trace-status-${action.status}`}>
                      {action.status}
                    </span>
                  </article>
                ))}
              </div>
            )}

            <h4 className="growth-sub">{t('agentDetail.skillsLearned')}</h4>
            {learned && learned.length === 0 && (
              <EmptyState>{t('agentDetail.noLearnedSkills')}</EmptyState>
            )}
            {(learned ?? []).map((skill) => (
              <article className="skill-card" key={skill.name}>
                <div>
                  {materialIcon('school')}
                  <strong>{skillTitle(skill.content, skill.name)}</strong>
                </div>
                <p>{skill.content.replace(/^#+\s*.*\n+/, '').slice(0, 200)}</p>
              </article>
            ))}
          </section>

          <section className="drawer-section">
            <h3>{t('agentDetail.experience')}</h3>
            {agent.experiences.length === 0 && (
              <EmptyState>{t('agentDetail.noExperience')}</EmptyState>
            )}
            {agent.experiences.slice(0, 5).map((experience) => (
              <article
                className={`experience-card ${experience.outcome}`}
                key={experience.id}
              >
                <div>
                  {materialIcon(
                    experience.outcome === 'success'
                      ? 'check_circle'
                      : 'report',
                  )}
                  <strong>
                    {experience.outcome === 'success' ? t('agentDetail.successExperience') : t('agentDetail.lessonLearned')}
                  </strong>
                  <span>{experience.time}</span>
                </div>
                <p>{experience.summary}</p>
                {experience.lessons && <em>{experience.lessons}</em>}
              </article>
            ))}
          </section>

          <section className="drawer-section">
            <div className="growth-head">
              <h3>认知记忆与证据</h3>
              <span className="drawer-section-note">原始公司记录可回溯</span>
            </div>
            {relationships.length > 0 && (
              <div className="business-policy-list">
                {relationships.slice(0, 4).map((relationship) => (
                  <div key={`${relationship.colleague_name}-${relationship.summary}`}>
                    <span>
                      {materialIcon('groups')}
                      <strong>{relationship.colleague_name}</strong>
                      <em>{relationship.summary}</em>
                    </span>
                    <small>{Math.round(relationship.trust_score * 100)}%</small>
                  </div>
                ))}
              </div>
            )}
            {memories.length === 0 && <EmptyState>暂无可审计记忆</EmptyState>}
            {memories.slice(0, 8).map((memory) => (
              <article className="experience-card" key={memory.id}>
                <div>
                  {materialIcon(memory.memory_type === 'reflection' ? 'psychology' : 'fact_check')}
                  <strong>{memory.title || memory.memory_type}</strong>
                  <span>{memory.is_private ? '员工私有判断' : '公司共享经验'}</span>
                </div>
                <p>{memory.content}</p>
                <em>
                  证据 {memory.evidence_event_ids.length} 条
                  {memory.evidence_event_ids.slice(0, 2).map((eventId) => {
                    const event = companyEvents.find((item) => item.id === eventId);
                    return event ? ` · ${event.actor_name}：${event.title}` : '';
                  }).join('')}
                  {' · '}可信度 {Math.round(memory.confidence * 100)}%
                </em>
              </article>
            ))}
          </section>

          <section className="drawer-section">
            <h3>{t('agentDetail.relatedTasks')}</h3>
            {tasks.length === 0 && <EmptyState>{t('agentDetail.noTasks')}</EmptyState>}
            {tasks.map((task) => {
              const status = statusStyle(task.status);
              const priority = priorityStyle(task.pr);
              return (
                <article className="drawer-task" key={task.id}>
                  <div>
                    <em style={priority}>{task.pr}</em>
                    <strong>{task.title}</strong>
                  </div>
                  <div className="drawer-progress">
                    <div className="progress-track">
                      <i
                        style={{
                          width: `${task.progress}%`,
                          background: status.bar,
                        }}
                      />
                    </div>
                    <span>{task.status}</span>
                  </div>
                </article>
              );
            })}
          </section>
        </div>
      </aside>
    </>
  );
}

function TaskDetail({
  task,
  token,
  plan,
  agent,
  onClose,
  onOpenChat,
  onOpenAgent,
  onAdvanceTask,
  onResolveApproval,
}: {
  task: Task;
  token: string | null;
  plan?: TaskPlanSnapshot;
  agent?: Agent;
  onClose: () => void;
  onOpenChat: (id: string) => void;
  onOpenAgent: (id: string) => void;
  onAdvanceTask: (task: Task) => void;
  onResolveApproval: (
    approval: Approval,
    status: 'approved' | 'rejected',
  ) => void;
}) {
  const { t } = useTranslation();
  const planTask = plan?.tasks.find((item) => item.id === task.id);
  const effectiveStatus = normalizeTaskStatus(planTask?.status ?? task.status);
  const status = statusStyle(effectiveStatus);
  const priority = priorityStyle(task.pr);
  const [runs, setRuns] = useState<TaskRunTrack[]>([]);
  const [resumeMessage, setResumeMessage] = useState('');
  const [resumeError, setResumeError] = useState('');
  const [resuming, setResuming] = useState(false);
  const pendingApprovals = task.approvals.filter(
    (approval) => approval.status === 'pending',
  );
  const latestOutput = task.outputs[0];
  const planOutput = planTask?.outputs.at(-1);
  const packageOutput = planTask?.outputs
    .filter((output) => output.output_type === 'content_package_v1')
    .at(-1);
  const taskBusinessActions =
    planTask?.business_actions ?? runs.flatMap((run) => run.business_actions ?? []);
  const contentPackage = parseContentPackage(packageOutput?.content);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    const refreshRuns = async () => {
      try {
        const nextRuns = await apiRequest<TaskRunTrack[]>(`/tasks/${task.id}/runs`, {
          token,
        });
        if (!cancelled) setRuns(nextRuns);
      } catch {
        // Keep the latest successful trace visible during transient failures.
      }
    };
    void refreshRuns();
    const timer = window.setInterval(refreshRuns, 3000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [task.id, token]);

  const resumeBlockedTask = async () => {
    if (!token || !resumeMessage.trim()) return;
    setResuming(true);
    setResumeError('');
    try {
      await apiRequest(`/tasks/${task.id}/resume`, {
        token,
        method: 'POST',
        body: JSON.stringify({ message: resumeMessage.trim() }),
      });
      setResumeMessage('');
    } catch (error) {
      setResumeError(error instanceof Error ? error.message : '恢复失败');
    } finally {
      setResuming(false);
    }
  };

  return (
    <>
      <div className="overlay" onClick={onClose} />
      <aside className="agent-drawer task-detail-drawer" aria-label={t('taskDetail.aria')}>
        <header>
          <div className="drawer-topline">
            <span className="priority-pill" style={priority}>
              {task.pr}
            </span>
            <button type="button" title={t('common.close')} onClick={onClose}>
              {materialIcon('close')}
            </button>
          </div>
          <h2>
            {task.title}
            <span style={{ color: status.color }}>
              <i style={{ background: status.bar }} />
              {effectiveStatus}
            </span>
          </h2>
          <p>{task.description || t('taskDetail.noDescription')}</p>
          <div className="drawer-actions">
            {task.src && (
              <button
                className="button secondary"
                type="button"
                onClick={() => onOpenChat(task.src)}
              >
                {materialIcon('forum')}{t('taskDetail.relatedConversation')}
              </button>
            )}
            {!task.taskPlanId && (
              <button
                className="button primary"
                type="button"
                onClick={() => onAdvanceTask(task)}
              >
                {effectiveStatus === '已完成' ? effectiveStatus : t('taskDetail.advance')}
              </button>
            )}
          </div>
        </header>

        <div className="drawer-scroll">
          <section className="drawer-section">
            <h3>{t('taskDetail.overview')}</h3>
            <div className="task-detail-grid">
              <div>
                <span>{t('tasks.colOwner')}</span>
                {agent ? (
                  <button type="button" onClick={() => onOpenAgent(agent.id)}>
                    {avatarText(agent.name)} · {agent.name}
                  </button>
                ) : (
                  <strong>{t('tasks.unassigned')}</strong>
                )}
              </div>
              <div>
                <span>{t('taskDetail.relatedConversation')}</span>
                <strong>{task.srcLabel}</strong>
              </div>
              <div>
                <span>{t('taskDetail.createdAt')}</span>
                <strong>{task.createdAt}</strong>
              </div>
              <div>
                <span>{t('taskDetail.updatedAt')}</span>
                <strong>{task.updatedAt}</strong>
              </div>
            </div>
            <div className="task-detail-progress">
              <div className="progress-track">
                <i
                  style={{
                  width: `${planTask?.progress ?? task.progress}%`,
                    background: status.bar,
                  }}
                />
              </div>
              <span>{planTask?.progress ?? task.progress}%</span>
            </div>
          </section>

          {effectiveStatus === '阻塞' && (
            <section className="drawer-section task-resume-panel">
              <h3>需要老板补充</h3>
              <p>{planTask?.blocked_reason || plan?.blocked_reason || '任务已阻塞，请补充执行所需信息。'}</p>
              <textarea
                value={resumeMessage}
                onChange={(event) => setResumeMessage(event.target.value)}
                placeholder="补充缺失信息或说明如何继续"
              />
              {resumeError && <span className="form-error">{resumeError}</span>}
              <button
                className="button primary"
                type="button"
                disabled={resuming || !resumeMessage.trim()}
                onClick={resumeBlockedTask}
              >
                {materialIcon('play_arrow')}{resuming ? '恢复中…' : '恢复执行'}
              </button>
            </section>
          )}

          {pendingApprovals.length > 0 && (
            <section className="drawer-section">
              <h3>{t('taskDetail.pendingApproval')}</h3>
              {pendingApprovals.map((approval) => (
                <article className="approval-request" key={approval.id}>
                  <div>
                    {materialIcon('approval', 'warning')}
                    <span>
                      <strong>{approval.title}</strong>
                      <em>{approval.description}</em>
                    </span>
                  </div>
                  <footer>
                    <button
                      className="button secondary"
                      type="button"
                      onClick={() => onResolveApproval(approval, 'rejected')}
                    >
                      {t('taskDetail.reject')}
                    </button>
                    <button
                      className="button primary"
                      type="button"
                      onClick={() => onResolveApproval(approval, 'approved')}
                    >
                      {t('taskDetail.approve')}
                    </button>
                  </footer>
                </article>
              ))}
            </section>
          )}

          <section className="drawer-section">
            <h3>{t('taskDetail.latestOutput')}</h3>
            {contentPackage ? (
              <ContentPackagePreview value={contentPackage} />
            ) : planOutput ? (
              <article className="task-output-card">
                <header>
                  <strong>{planOutput.title}</strong>
                  <span>{formatTime(planOutput.created_at)}</span>
                </header>
                <pre>
                  {typeof planOutput.content === 'string'
                    ? planOutput.content
                    : JSON.stringify(planOutput.content, null, 2)}
                </pre>
              </article>
            ) : latestOutput ? (
              <article className="task-output-card">
                <header>
                  <strong>{latestOutput.title}</strong>
                  <span>{latestOutput.time}</span>
                </header>
                <pre>{latestOutput.content}</pre>
              </article>
            ) : (
              <EmptyState>{t('taskDetail.noOutput')}</EmptyState>
            )}
          </section>

          <section className="drawer-section">
            <h3>Run 轨迹</h3>
            {runs.length === 0 ? (
              <EmptyState>尚无运行记录</EmptyState>
            ) : (
              <div className="task-run-list">
                {runs.map((run) => (
                  <article className="task-run-card" key={run.id}>
                    <header>
                      <strong>第 {run.attempt_no} 次执行</strong>
                      <span className={`run-trace-status run-trace-status-${run.status}`}>
                        {run.status}
                      </span>
                    </header>
                    {run.error && <p className="run-trace-error">{run.error}</p>}
                    <ol>
                      {run.steps.map((step) => (
                        <li key={step.id}>
                          <b>{step.title || step.type}</b>
                          {step.detail && <span>{step.detail}</span>}
                        </li>
                      ))}
                    </ol>
                  </article>
                ))}
              </div>
            )}
          </section>

          {taskBusinessActions.length > 0 && (
            <section className="drawer-section">
              <h3>对外业务动作</h3>
              <div className="business-action-list">
                {taskBusinessActions.map((action) => (
                  <article key={action.id}>
                    <div>
                      {materialIcon(action.status === 'succeeded' ? 'check_circle' : 'send')}
                      <span>
                        <strong>{action.preview.subject || action.tool_name}</strong>
                        <em>{action.preview.to?.join('、') || action.provider || action.tool_name}</em>
                      </span>
                    </div>
                    <span className={`run-trace-status run-trace-status-${action.status}`}>
                      {action.status}
                    </span>
                  </article>
                ))}
              </div>
            </section>
          )}

          <section className="drawer-section">
            <h3>{t('taskDetail.executionLog')}</h3>
            <div className="task-timeline">
              {task.events.length === 0 && (
                <EmptyState>{t('taskDetail.noEvents')}</EmptyState>
              )}
              {task.events.map((event) => (
                <article className="timeline-event" key={event.id}>
                  <i />
                  <div>
                    <strong>{event.title}</strong>
                    <span>{event.time}</span>
                    {event.content && <p>{event.content}</p>}
                  </div>
                </article>
              ))}
            </div>
          </section>
        </div>
      </aside>
    </>
  );
}

function parseContentPackage(value: unknown): ContentPackageData | null {
  if (!value) return null;
  try {
    const parsed = typeof value === 'string' ? JSON.parse(value) : value;
    if (
      typeof parsed === 'object' &&
      parsed !== null &&
      'platform' in parsed &&
      'schedule' in parsed &&
      Array.isArray((parsed as ContentPackageData).schedule)
    ) {
      return parsed as ContentPackageData;
    }
  } catch {
    return null;
  }
  return null;
}

function contentPackageToMarkdown(value: ContentPackageData) {
  const lines = [
    `# ${value.platform} 内容发布计划`,
    '',
    `- 受众：${value.audience}`,
    `- 目标：${value.objective}`,
    '',
    '## 发布日历',
  ];
  [...value.schedule]
    .sort((left, right) => left.order - right.order)
    .forEach((item) => {
      lines.push(
        '',
        `### ${item.order}. ${item.title}`,
        `- 发布时间：${item.publish_at}`,
        `- 类型：${item.content_type}`,
        `- 开场钩子：${item.hook}`,
        `- CTA：${item.cta}`,
        `- 素材建议：${item.asset_suggestion}`,
        `- 来源：${item.source_refs.join('、') || '无'}`,
        '',
        item.body || item.script || '',
      );
    });
  lines.push('', '## 来源');
  value.sources.forEach((source) =>
    lines.push(`- ${source.title || source.id || source.url}: ${source.id || source.url}`),
  );
  lines.push('', '## 假设与未知项');
  value.assumptions.forEach((assumption) => lines.push(`- ${assumption}`));
  return `${lines.join('\n')}\n`;
}

function ContentPackagePreview({ value }: { value: ContentPackageData }) {
  const exportMarkdown = () => {
    const blob = new Blob([contentPackageToMarkdown(value)], {
      type: 'text/markdown;charset=utf-8',
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `${value.platform}-内容计划.md`;
    anchor.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="content-package">
      <header>
        <div>
          <strong>{value.platform}</strong>
          <span>{value.audience} · {value.objective}</span>
        </div>
        <button className="button secondary" type="button" onClick={exportMarkdown}>
          {materialIcon('download')}导出 Markdown
        </button>
      </header>
      <div className="publish-calendar">
        {[...value.schedule]
          .sort((left, right) => left.order - right.order)
          .map((item) => (
            <article key={`${item.order}-${item.title}`}>
              <div className="publish-date">
                <b>{item.order}</b>
                <span>{item.publish_at}</span>
              </div>
              <div>
                <em>{item.content_type}</em>
                <h4>{item.title}</h4>
                <strong>{item.hook}</strong>
                <p>{item.body || item.script}</p>
                <dl>
                  <dt>CTA</dt><dd>{item.cta}</dd>
                  <dt>素材</dt><dd>{item.asset_suggestion}</dd>
                  <dt>来源</dt><dd>{item.source_refs.join('、') || '无'}</dd>
                </dl>
              </div>
            </article>
          ))}
      </div>
      <section className="content-package-notes">
        <div>
          <h4>来源</h4>
          {value.sources.length === 0 ? (
            <p>无</p>
          ) : (
            value.sources.map((source) => (
              <p key={source.id || source.url}>
                {source.url ? (
                  <a href={source.url} target="_blank" rel="noreferrer">
                    {source.title || source.url}
                  </a>
                ) : (
                  `${source.title || source.id} · ${source.id}`
                )}
              </p>
            ))
          )}
        </div>
        <div>
          <h4>假设与未知项</h4>
          {value.assumptions.length === 0 ? (
            <p>无</p>
          ) : (
            value.assumptions.map((assumption) => <p key={assumption}>{assumption}</p>)
          )}
        </div>
      </section>
    </div>
  );
}

function HireModal({
  template,
  departments,
  hireDept,
  onDeptChange,
  onClose,
  onSubmit,
}: {
  template?: HireTemplate;
  departments: string[];
  hireDept: string;
  onDeptChange: (dept: string) => void;
  onClose: () => void;
  onSubmit: () => void;
}) {
  const { t } = useTranslation();
  if (!template) return null;

  return (
    <Modal
      title={t('hireModal.title')}
      description={t('hireModal.description')}
      width={560}
      onClose={onClose}
    >
      <div className="hire-summary">
        <div
          className="large-avatar"
          style={{ background: 'oklch(0.55 0.11 230)' }}
        >
          {avatarText(template.name)}
        </div>
        <div>
          <strong>{template.name}</strong>
          <p>
            {template.dept} · {template.desc}
          </p>
        </div>
      </div>

      <FieldLabel>{t('hireModal.dept')}</FieldLabel>
      <input
        value={hireDept}
        placeholder={t('hireModal.deptPlaceholder', { dept: template.dept })}
        onChange={(event) => onDeptChange(event.currentTarget.value)}
      />
      {departments.length > 0 && (
        <>
          <FieldLabel>{t('hireModal.existingDepts')}</FieldLabel>
          <div className="chip-row compact">
            {departments.map((department) => (
              <button
                className={
                  hireDept === department
                    ? 'selector-chip active'
                    : 'selector-chip'
                }
                key={department}
                type="button"
                onClick={() => onDeptChange(department)}
              >
                {department}
              </button>
            ))}
          </div>
        </>
      )}

      <FieldLabel>{t('hireModal.rolePrompt')}</FieldLabel>
      <textarea readOnly rows={5} value={template.prompt} />

      <FieldLabel>{t('hireModal.capabilityTags')}</FieldLabel>
      <ChipList items={template.skills} emptyText={t('market.noSkills')} />

      <div className="modal-actions">
        <button className="button secondary" type="button" onClick={onClose}>
          {t('common.cancel')}
        </button>
        <button className="button primary" type="button" onClick={onSubmit}>
          {materialIcon('person_add')}{t('hireModal.confirm')}
        </button>
      </div>
    </Modal>
  );
}

function CreateTaskModal({
  agents,
  chats,
  taskTitle,
  taskDesc,
  taskPriority,
  taskOwnerId,
  taskConversationId,
  agentById,
  onTitleChange,
  onDescChange,
  onPriorityChange,
  onOwnerChange,
  onConversationChange,
  onClose,
  onSubmit,
}: {
  agents: Agent[];
  chats: Chat[];
  taskTitle: string;
  taskDesc: string;
  taskPriority: Priority;
  taskOwnerId: string;
  taskConversationId: string;
  agentById: (id: string) => Agent | undefined;
  onTitleChange: (value: string) => void;
  onDescChange: (value: string) => void;
  onPriorityChange: (value: Priority) => void;
  onOwnerChange: (value: string) => void;
  onConversationChange: (value: string) => void;
  onClose: () => void;
  onSubmit: () => void;
}) {
  const { t } = useTranslation();
  return (
    <Modal
      title={t('createTask.title')}
      description={t('createTask.description')}
      width={680}
      onClose={onClose}
    >
      <FieldLabel>{t('createTask.taskTitle')}</FieldLabel>
      <input
        value={taskTitle}
        placeholder={t('createTask.taskTitlePlaceholder')}
        onChange={(event) => onTitleChange(event.currentTarget.value)}
      />

      <FieldLabel>{t('createTask.taskDesc')}</FieldLabel>
      <textarea
        rows={4}
        value={taskDesc}
        placeholder={t('createTask.taskDescPlaceholder')}
        onChange={(event) => onDescChange(event.currentTarget.value)}
      />

      <div className="form-grid even">
        <label>
          <FieldLabel>{t('tasks.colOwner')}</FieldLabel>
          <select
            value={taskOwnerId}
            onChange={(event) => onOwnerChange(event.currentTarget.value)}
          >
            <option value="">{t('tasks.unassigned')}</option>
            {agents.map((agent) => (
              <option key={agent.id} value={agent.id}>
                {agent.name} · {agent.role}
              </option>
            ))}
          </select>
        </label>
        <label>
          <FieldLabel>{t('createTask.relatedConversation')}</FieldLabel>
          <select
            value={taskConversationId}
            onChange={(event) =>
              onConversationChange(event.currentTarget.value)
            }
          >
            <option value="">{t('createTask.noRelated')}</option>
            {chats.map((chat) => (
              <option key={chat.id} value={chat.id}>
                {chat.kind === 'group'
                  ? `# ${chat.name}`
                  : t('createTask.dmOption', { name: agentById(chat.agentId)?.name ?? t('createTask.employeeFallback') })}
              </option>
            ))}
          </select>
        </label>
      </div>

      <FieldLabel>{t('createTask.priority')}</FieldLabel>
      <div className="chip-row compact">
        {(['P0', 'P1', 'P2'] as const).map((priority) => (
          <button
            className={
              taskPriority === priority
                ? 'selector-chip active'
                : 'selector-chip'
            }
            key={priority}
            type="button"
            onClick={() => onPriorityChange(priority)}
          >
            {priority}
          </button>
        ))}
      </div>

      <div className="modal-actions">
        <button className="button secondary" type="button" onClick={onClose}>
          {t('common.cancel')}
        </button>
        <button className="button primary" type="button" onClick={onSubmit}>
          {materialIcon('add_task')}{t('tasks.create')}
        </button>
      </div>
    </Modal>
  );
}

function TeamCompilerModal({
  token,
  description,
  onDescriptionChange,
  drafts,
  loading,
  error,
  creating,
  onDraft,
  onUpdateMember,
  onRemoveMember,
  onBack,
  onConfirm,
  onClose,
}: {
  token: string;
  description: string;
  onDescriptionChange: (value: string) => void;
  drafts: TeamMemberDraft[] | null;
  loading: boolean;
  error: string;
  creating: boolean;
  onDraft: () => void;
  onUpdateMember: (index: number, patch: Partial<TeamMemberDraft>) => void;
  onRemoveMember: (index: number) => void;
  onBack: () => void;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [catalog, setCatalog] = useState<
    { key: string; description: string; risk_gate: string }[] | null
  >(null);
  const [pickerOpenFor, setPickerOpenFor] = useState<number | null>(null);
  const [pickerKey, setPickerKey] = useState('');

  useEffect(() => {
    if (!token || catalog !== null) return;
    apiRequest<{ key: string; description: string; risk_gate: string }[]>(
      '/capabilities',
      { token },
    )
      .then(setCatalog)
      .catch(() => setCatalog([]));
  }, [token, catalog]);

  const openPicker = (index: number) => {
    setPickerKey('');
    setPickerOpenFor(index);
  };

  const addCapability = (index: number) => {
    if (!pickerKey) return;
    const member = drafts?.[index];
    if (!member || member.capability_keys.includes(pickerKey)) return;
    onUpdateMember(index, { capability_keys: [...member.capability_keys, pickerKey] });
    setPickerOpenFor(null);
    setPickerKey('');
  };

  const removeCapability = (index: number, key: string) => {
    const member = drafts?.[index];
    if (!member) return;
    onUpdateMember(index, {
      capability_keys: member.capability_keys.filter((k) => k !== key),
    });
  };

  return (
    <Modal
      title={t('teamCompiler.title')}
      description={t('teamCompiler.description')}
      width={780}
      onClose={onClose}
    >
      {drafts === null ? (
        <>
          <FieldLabel>{t('teamCompiler.describeLabel')}</FieldLabel>
          <textarea
            rows={10}
            value={description}
            placeholder={t('teamCompiler.describePlaceholder')}
            onChange={(event) => onDescriptionChange(event.currentTarget.value)}
          />
          {error && <div className="auth-error">{error}</div>}
          <div className="modal-actions">
            <button className="button secondary" type="button" onClick={onClose}>
              {t('common.cancel')}
            </button>
            <button
              className="button primary"
              type="button"
              disabled={loading || !description.trim()}
              onClick={onDraft}
            >
              {materialIcon('auto_awesome')}
              {loading ? t('teamCompiler.drafting') : t('teamCompiler.draftSubmit')}
            </button>
          </div>
        </>
      ) : (
        <>
          <div className="team-draft-list">
            {drafts.map((member, index) => (
              <div className="team-draft-card" key={index}>
                <div className="modal-actions" style={{ justifyContent: 'space-between' }}>
                  <FieldLabel>{t('teamCompiler.memberN', { n: index + 1 })}</FieldLabel>
                  <button
                    type="button"
                    className="button small"
                    title={t('teamCompiler.removeMember')}
                    onClick={() => onRemoveMember(index)}
                  >
                    {materialIcon('delete')}
                  </button>
                </div>
                <div className="form-grid even">
                  <label>
                    <FieldLabel>{t('createAgent.name')}</FieldLabel>
                    <input
                      value={member.name}
                      onChange={(event) =>
                        onUpdateMember(index, { name: event.currentTarget.value })
                      }
                    />
                  </label>
                  <label>
                    <FieldLabel>{t('teamCompiler.role')}</FieldLabel>
                    <input
                      value={member.role}
                      onChange={(event) =>
                        onUpdateMember(index, { role: event.currentTarget.value })
                      }
                    />
                  </label>
                </div>
                <label>
                  <FieldLabel>{t('createAgent.dept')}</FieldLabel>
                  <input
                    value={member.department}
                    onChange={(event) =>
                      onUpdateMember(index, { department: event.currentTarget.value })
                    }
                  />
                </label>
                <label>
                  <FieldLabel>{t('createAgent.employeeDesc')}</FieldLabel>
                  <input
                    value={member.description}
                    onChange={(event) =>
                      onUpdateMember(index, { description: event.currentTarget.value })
                    }
                  />
                </label>
                <label>
                  <FieldLabel>{t('teamCompiler.responsibilities')}</FieldLabel>
                  <textarea
                    rows={3}
                    value={member.responsibilities.join('\n')}
                    placeholder={t('teamCompiler.responsibilitiesPlaceholder')}
                    onChange={(event) =>
                      onUpdateMember(index, {
                        responsibilities: event.currentTarget.value
                          .split('\n')
                          .map((line) => line.trim())
                          .filter(Boolean),
                      })
                    }
                  />
                </label>

                <FieldLabel>{t('createAgent.capabilities')}</FieldLabel>
                <div className="chip-list">
                  {member.capability_keys.map((key) => (
                    <span className="chip" key={key}>
                      {key}
                      <button
                        type="button"
                        title={t('teamCompiler.removeCapability')}
                        onClick={() => removeCapability(index, key)}
                      >
                        {materialIcon('close')}
                      </button>
                    </span>
                  ))}
                  <button
                    type="button"
                    className="button small"
                    onClick={() => openPicker(index)}
                  >
                    {t('teamCompiler.addCapability')}
                  </button>
                </div>
                {pickerOpenFor === index && (
                  <div className="grant-capability-picker">
                    {catalog === null ? null : (
                      <>
                        <label>
                          {t('agentDetail.grantCapabilityPick')}
                          <select
                            value={pickerKey}
                            onChange={(e) => setPickerKey(e.target.value)}
                          >
                            <option value="" disabled>
                              —
                            </option>
                            {catalog
                              .filter((entry) => !member.capability_keys.includes(entry.key))
                              .map((entry) => (
                                <option key={entry.key} value={entry.key}>
                                  {entry.key} — {entry.description}
                                </option>
                              ))}
                          </select>
                        </label>
                        <div className="drawer-actions">
                          <button
                            type="button"
                            className="button primary small"
                            disabled={!pickerKey}
                            onClick={() => addCapability(index)}
                          >
                            {t('agentDetail.grantCapabilityConfirm')}
                          </button>
                          <button
                            type="button"
                            className="button small"
                            onClick={() => setPickerOpenFor(null)}
                          >
                            {t('agentDetail.grantCapabilityCancel')}
                          </button>
                        </div>
                      </>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>

          <div className="modal-actions">
            <button className="button secondary" type="button" onClick={onBack}>
              {t('teamCompiler.backToDescribe')}
            </button>
            <button
              className="button primary"
              type="button"
              disabled={creating || drafts.length === 0}
              onClick={onConfirm}
            >
              {materialIcon('add_circle')}
              {creating ? t('teamCompiler.creating') : t('teamCompiler.confirmSubmit')}
            </button>
          </div>
        </>
      )}
    </Modal>
  );
}

const CAPABILITY_OPTIONS = [
  { key: 'write_code', risk: 'auto' },
  { key: 'run_tests', risk: 'auto' },
  { key: 'git_push', risk: 'approval' },
  { key: 'deploy_preview', risk: 'auto' },
  { key: 'deploy_prod', risk: 'approval' },
  { key: 'domain_register', risk: 'prohibited_auto' },
  { key: 'seo_audit', risk: 'auto' },
  { key: 'social_content', risk: 'approval' },
] as const;

const RISK_BADGE_COLOR: Record<string, string> = {
  auto: '#4caf50',
  approval: '#ff9800',
  prohibited_auto: '#f44336',
};

type RoleBundle = {
  role_name: string;
  capability_keys: string[];
  resolved: { risk_gate: string };
};

function CreateAgentModal({
  token,
  departments,
  createName,
  createDesc,
  createDept,
  createPrompt,
  createCapKeys,
  onNameChange,
  onDescChange,
  onDeptChange,
  onPromptChange,
  onCapKeysChange,
  onClose,
  onSubmit,
}: {
  token: string;
  departments: string[];
  createName: string;
  createDesc: string;
  createDept: string;
  createPrompt: string;
  createCapKeys: string[];
  onNameChange: (value: string) => void;
  onDescChange: (value: string) => void;
  onDeptChange: (value: string) => void;
  onPromptChange: (value: string) => void;
  onCapKeysChange: (value: string[]) => void;
  onClose: () => void;
  onSubmit: () => void;
}) {
  const { t } = useTranslation();
  const [bundles, setBundles] = useState<RoleBundle[]>([]);

  useEffect(() => {
    if (!token) return;
    let alive = true;
    apiRequest<RoleBundle[]>('/role-bundles', { token })
      .then((data) => {
        if (alive) setBundles(data);
      })
      .catch(() => {
        /* catalog unavailable — manual capability picking still works */
      });
    return () => {
      alive = false;
    };
  }, [token]);

  const toggleCap = (key: string) => {
    onCapKeysChange(
      createCapKeys.includes(key)
        ? createCapKeys.filter((k) => k !== key)
        : [...createCapKeys, key],
    );
  };

  const bundleActive = (bundle: RoleBundle) =>
    bundle.capability_keys.length === createCapKeys.length &&
    bundle.capability_keys.every((key) => createCapKeys.includes(key));

  const pickBundle = (bundle: RoleBundle) => {
    onCapKeysChange(bundleActive(bundle) ? [] : [...bundle.capability_keys]);
    if (!createName.trim()) onNameChange(bundle.role_name);
    if (!createDept.trim()) onDeptChange(bundle.role_name);
  };

  return (
    <Modal
      title={t('createAgent.title')}
      description={t('createAgent.description')}
      width={720}
      onClose={onClose}
    >
      <div className="form-grid even">
        <label>
          <FieldLabel>{t('createAgent.name')}</FieldLabel>
          <input
            value={createName}
            placeholder={t('createAgent.namePlaceholder')}
            onChange={(event) => onNameChange(event.currentTarget.value)}
          />
        </label>
        <label>
          <FieldLabel>{t('createAgent.dept')}</FieldLabel>
          <input
            value={createDept}
            list="agentpulse-departments"
            placeholder={t('createAgent.deptPlaceholder')}
            onChange={(event) => onDeptChange(event.currentTarget.value)}
          />
          <datalist id="agentpulse-departments">
            {departments.map((department) => (
              <option key={department} value={department} />
            ))}
          </datalist>
        </label>
      </div>

      <FieldLabel>{t('createAgent.employeeDesc')}</FieldLabel>
      <input
        value={createDesc}
        placeholder={t('createAgent.employeeDescPlaceholder')}
        onChange={(event) => onDescChange(event.currentTarget.value)}
      />

      {bundles.length > 0 && (
        <>
          <FieldLabel>{t('createAgent.quickConfig')}</FieldLabel>
          <div className="role-bundle-row">
            {bundles.map((bundle) => (
              <button
                key={bundle.role_name}
                type="button"
                className={
                  bundleActive(bundle)
                    ? 'role-bundle-chip active'
                    : 'role-bundle-chip'
                }
                onClick={() => pickBundle(bundle)}
              >
                {bundle.role_name}
              </button>
            ))}
          </div>
        </>
      )}

      <FieldLabel>{t('createAgent.capabilities')}</FieldLabel>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 8 }}>
        {CAPABILITY_OPTIONS.map((cap) => {
          const selected = createCapKeys.includes(cap.key);
          const badgeColor = RISK_BADGE_COLOR[cap.risk];
          return (
            <button
              key={cap.key}
              type="button"
              title={t(`capability.${cap.key}.desc`)}
              className={selected ? 'cap-chip selected' : 'cap-chip'}
              onClick={() => toggleCap(cap.key)}
            >
              <span>{t(`capability.${cap.key}.label`)}</span>
              <span
                className="risk-badge"
                style={{ background: badgeColor + '22', color: badgeColor }}
              >
                {t(`createAgent.risk.${cap.risk}`)}
              </span>
            </button>
          );
        })}
      </div>
      {createCapKeys.length > 0 && (
        <p className="cap-summary">
          {t('createAgent.selectedCount', {
            count: createCapKeys.length,
            keys: createCapKeys.join(' · '),
          })}
        </p>
      )}

      <FieldLabel>{t('market.rolePrompt')}</FieldLabel>
      <textarea
        rows={8}
        value={createPrompt}
        placeholder={t('createAgent.promptPlaceholder')}
        onChange={(event) => onPromptChange(event.currentTarget.value)}
      />

      <div className="modal-actions">
        <button className="button secondary" type="button" onClick={onClose}>
          {t('common.cancel')}
        </button>
        <button className="button primary" type="button" onClick={onSubmit}>
          {materialIcon('add_circle')}{t('createAgent.submit')}
        </button>
      </div>
    </Modal>
  );
}

function ClaimTaskModal({
  task,
  agents,
  selectedAgentId,
  onAgentChange,
  onClose,
  onSubmit,
}: {
  task?: Task;
  agents: Agent[];
  selectedAgentId: string;
  onAgentChange: (value: string) => void;
  onClose: () => void;
  onSubmit: () => void;
}) {
  const { t } = useTranslation();
  const suggestedAgent = task?.suggestedAgentId
    ? agents.find((agent) => agent.id === task.suggestedAgentId)
    : null;

  return (
    <Modal
      title={t('claimTask.title')}
      description={task ? t('claimTask.descriptionWithTitle', { title: task.title }) : t('claimTask.description')}
      width={520}
      onClose={onClose}
    >
      <FieldLabel>{t('claimTask.assignee')}</FieldLabel>
      {suggestedAgent && (
        <div className="claim-suggestion">
          <div
            className="tiny-avatar"
            style={{ background: avatarColor(suggestedAgent) }}
          >
            {avatarText(suggestedAgent.name)}
          </div>
          <span>
            <strong>{t('tasks.suggested', { name: suggestedAgent.name })}</strong>
            <p>{task?.suggestedAgentReason || t('claimTask.defaultReason')}</p>
          </span>
        </div>
      )}
      <select
        value={selectedAgentId}
        onChange={(event) => onAgentChange(event.currentTarget.value)}
      >
        <option value="">{t('claimTask.selectEmployee')}</option>
        {agents.map((agent) => (
          <option key={agent.id} value={agent.id}>
            {agent.name} · {agent.role}
          </option>
        ))}
      </select>
      <div className="market-admin-note">{t('claimTask.note')}</div>
      <div className="modal-actions">
        <button className="button secondary" type="button" onClick={onClose}>
          {t('common.cancel')}
        </button>
        <button className="button primary" type="button" onClick={onSubmit}>
          {materialIcon('assignment_ind')}{t('claimTask.confirm')}
        </button>
      </div>
    </Modal>
  );
}

function KnowledgeSourceModal({
  title,
  category,
  content,
  onTitleChange,
  onCategoryChange,
  onContentChange,
  onClose,
  onSubmit,
}: {
  title: string;
  category: string;
  content: string;
  onTitleChange: (value: string) => void;
  onCategoryChange: (value: string) => void;
  onContentChange: (value: string) => void;
  onClose: () => void;
  onSubmit: () => void;
}) {
  const { t } = useTranslation();
  return (
    <Modal
      title={t('knowledgeModal.title')}
      description={t('knowledgeModal.description')}
      width={640}
      onClose={onClose}
    >
      <div className="form-grid even">
        <label>
          <FieldLabel>{t('knowledgeModal.sourceTitle')}</FieldLabel>
          <input
            value={title}
            placeholder={t('knowledgeModal.sourceTitlePlaceholder')}
            onChange={(event) => onTitleChange(event.currentTarget.value)}
          />
        </label>
        <label>
          <FieldLabel>{t('knowledgeModal.category')}</FieldLabel>
          <select
            value={category}
            onChange={(event) => onCategoryChange(event.currentTarget.value)}
          >
            <option value="品牌资料">{t('knowledgeModal.categoryBrand')}</option>
            <option value="产品资料">{t('knowledgeModal.categoryProduct')}</option>
            <option value="客户资料">{t('knowledgeModal.categoryCustomer')}</option>
            <option value="运营记录">{t('knowledgeModal.categoryOps')}</option>
            <option value="通用资料">{t('knowledgeModal.categoryGeneral')}</option>
          </select>
        </label>
      </div>

      <FieldLabel>{t('knowledgeModal.content')}</FieldLabel>
      <textarea
        rows={9}
        value={content}
        placeholder={t('knowledgeModal.contentPlaceholder')}
        onChange={(event) => onContentChange(event.currentTarget.value)}
      />

      <div className="market-admin-note">{t('knowledgeModal.note')}</div>

      <div className="modal-actions">
        <button className="button secondary" type="button" onClick={onClose}>
          {t('common.cancel')}
        </button>
        <button className="button primary" type="button" onClick={onSubmit}>
          {materialIcon('save')}{t('knowledgeModal.save')}
        </button>
      </div>
    </Modal>
  );
}

function GroupModal({
  agents,
  tasks,
  groupName,
  groupMembers,
  selectedTaskIds,
  onGroupNameChange,
  onToggleMember,
  onToggleTask,
  onClose,
  onCreate,
}: {
  agents: Agent[];
  tasks: Task[];
  groupName: string;
  groupMembers: string[];
  selectedTaskIds: string[];
  onGroupNameChange: (value: string) => void;
  onToggleMember: (id: string) => void;
  onToggleTask: (id: string) => void;
  onClose: () => void;
  onCreate: () => void;
}) {
  const { t } = useTranslation();
  return (
    <Modal
      title={t('groupModal.title')}
      description={t('groupModal.description')}
      width={620}
      onClose={onClose}
    >
      <FieldLabel>{t('groupModal.name')}</FieldLabel>
      <input
        value={groupName}
        placeholder={t('groupModal.namePlaceholder')}
        onChange={(event) => onGroupNameChange(event.currentTarget.value)}
      />

      <FieldLabel>{t('groupModal.selectMembers')}</FieldLabel>
      <MemberPicker
        agents={agents}
        selectedMembers={groupMembers}
        onToggleMember={onToggleMember}
      />

      <FieldLabel>{t('groupModal.relatedTasks')}</FieldLabel>
      <TaskPicker
        tasks={tasks}
        selectedTaskIds={selectedTaskIds}
        onToggleTask={onToggleTask}
      />

      <div className="modal-actions">
        <button className="button secondary" type="button" onClick={onClose}>
          {t('common.cancel')}
        </button>
        <button className="button primary" type="button" onClick={onCreate}>
          {materialIcon('group_add')}{t('groupModal.create')}
        </button>
      </div>
    </Modal>
  );
}

function TaskPicker({
  tasks,
  selectedTaskIds,
  onToggleTask,
}: {
  tasks: Task[];
  selectedTaskIds: string[];
  onToggleTask: (id: string) => void;
}) {
  const { t } = useTranslation();
  if (!tasks.length) {
    return <div className="member-picker-empty">{t('groupModal.noTasks')}</div>;
  }

  return (
    <div className="task-picker">
      {tasks.map((task) => {
        const active = selectedTaskIds.includes(task.id);
        const status = statusStyle(task.status);
        return (
          <button
            className={active ? 'task-option active' : 'task-option'}
            key={task.id}
            type="button"
            onClick={() => onToggleTask(task.id)}
          >
            <span className="task-option-main">
              <strong>{task.title}</strong>
              <em>{task.srcLabel}</em>
            </span>
            <span
              className="status-pill"
              style={{ background: status.background, color: status.color }}
            >
              {task.status}
            </span>
            {materialIcon(active ? 'check_circle' : 'radio_button_unchecked')}
          </button>
        );
      })}
    </div>
  );
}

function GroupMembersModal({
  title,
  description,
  agents,
  selectedMembers,
  submitLabel,
  emptyText,
  onToggleMember,
  onClose,
  onSubmit,
}: {
  title: string;
  description: string;
  agents: Agent[];
  selectedMembers: string[];
  submitLabel: string;
  emptyText: string;
  onToggleMember: (id: string) => void;
  onClose: () => void;
  onSubmit: () => void;
}) {
  const { t } = useTranslation();
  return (
    <Modal
      title={title}
      description={description}
      width={620}
      onClose={onClose}
    >
      <FieldLabel>{t('groupModal.selectMembers')}</FieldLabel>
      <MemberPicker
        agents={agents}
        selectedMembers={selectedMembers}
        emptyText={emptyText}
        onToggleMember={onToggleMember}
      />

      <div className="modal-actions">
        <button className="button secondary" type="button" onClick={onClose}>
          {t('common.cancel')}
        </button>
        <button className="button primary" type="button" onClick={onSubmit}>
          {materialIcon('person_add')}
          {submitLabel}
        </button>
      </div>
    </Modal>
  );
}

function MemberPicker({
  agents,
  selectedMembers,
  emptyText,
  onToggleMember,
}: {
  agents: Agent[];
  selectedMembers: string[];
  emptyText?: string;
  onToggleMember: (id: string) => void;
}) {
  const { t } = useTranslation();
  if (!agents.length) {
    return <div className="member-picker-empty">{emptyText ?? t('groupModal.noEmployees')}</div>;
  }

  return (
    <div className="member-picker">
      {agents.map((agent) => {
        const active = selectedMembers.includes(agent.id);
        return (
          <button
            className={active ? 'member-option active' : 'member-option'}
            key={agent.id}
            type="button"
            onClick={() => onToggleMember(agent.id)}
          >
            <span
              className="tiny-avatar"
              style={{ background: avatarColor(agent) }}
            >
              {avatarText(agent.name)}
            </span>
            <span>
              <strong>{agent.name}</strong>
              <em>
                {agent.role} · {agent.dept}
              </em>
            </span>
            {materialIcon(active ? 'check_circle' : 'radio_button_unchecked')}
          </button>
        );
      })}
    </div>
  );
}

function OnboardingModal({
  step,
  templates,
  onNext,
  onFinish,
}: {
  step: number;
  templates: HireTemplate[];
  onNext: () => void;
  onFinish: () => void;
}) {
  const { t } = useTranslation();
  const featuredTemplates = templates.slice(0, 4);

  return (
    <>
      <div className="overlay blur" />
      <section className="onboarding-modal" aria-label={t('onboarding.aria')}>
        {step === 0 && (
          <div className="onboarding-content centered">
            <div className="onboarding-logo big" aria-hidden="true">
              <svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
                <path d="M16 4 L27 26 H21.5 L16 14 L10.5 26 H5 Z" fill="#06090c" fillOpacity=".92" />
                <rect x="10.5" y="18" width="11" height="4.5" rx="2.25" fill="#0d9488" />
                <circle cx="16" cy="20.25" r="2.25" fill="#eef4f2" />
              </svg>
            </div>
            <h2>{t('onboarding.welcomeTitle')}</h2>
            <p>{t('onboarding.welcomeBody')}</p>
            <div className="onboarding-feature-grid">
              <OnboardingFeature
                icon="forum"
                title={t('onboarding.feature1Title')}
                text={t('onboarding.feature1Text')}
              />
              <OnboardingFeature
                icon="storefront"
                title={t('onboarding.feature2Title')}
                text={t('onboarding.feature2Text')}
              />
              <OnboardingFeature
                icon="account_tree"
                title={t('onboarding.feature3Title')}
                text={t('onboarding.feature3Text')}
              />
            </div>
          </div>
        )}

        {step === 1 && (
          <div className="onboarding-content">
            <div className="onboarding-logo" aria-hidden="true">
              <svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
                <path d="M16 4 L27 26 H21.5 L16 14 L10.5 26 H5 Z" fill="#06090c" fillOpacity=".92" />
                <rect x="10.5" y="18" width="11" height="4.5" rx="2.25" fill="#0d9488" />
                <circle cx="16" cy="20.25" r="2.25" fill="#eef4f2" />
              </svg>
            </div>
            <h2>{t('onboarding.meetTitle')}</h2>
            <p>{t('onboarding.meetBody')}</p>
            <div className="role-grid">
              {featuredTemplates.map((template, index) => (
                <div className="role-option active" key={template.id}>
                  <div
                    style={{
                      background: `oklch(0.55 0.11 ${220 + index * 35})`,
                    }}
                  >
                    {['◆', '●', '▲', '■'][index % 4]}
                  </div>
                  <span>
                    <strong>{template.name}</strong>
                    <em>{template.dept}</em>
                  </span>
                  {materialIcon('check_circle')}
                </div>
              ))}
            </div>
          </div>
        )}

        {step === 2 && (
          <div className="onboarding-content centered">
            <div className="onboarding-logo big" aria-hidden="true">
              <svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
                <path d="M16 4 L27 26 H21.5 L16 14 L10.5 26 H5 Z" fill="#06090c" fillOpacity=".92" />
                <rect x="10.5" y="18" width="11" height="4.5" rx="2.25" fill="#0d9488" />
                <circle cx="16" cy="20.25" r="2.25" fill="#eef4f2" />
              </svg>
            </div>
            <h2>{t('onboarding.startTitle')}</h2>
            <p>{t('onboarding.startBody')}</p>
            <div className="try-chip">
              {materialIcon('chat')}
              {t('onboarding.tryChip')}
            </div>
          </div>
        )}

        <footer className="onboarding-footer">
          <div className="step-dots">
            {[0, 1, 2].map((item) => (
              <i className={step === item ? 'active' : ''} key={item} />
            ))}
          </div>
          <span />
          <button className="skip-button" type="button" onClick={onFinish}>
            {t('onboarding.skip')}
          </button>
          <button
            className="button primary"
            type="button"
            onClick={step >= 2 ? onFinish : onNext}
          >
            {step >= 2 ? t('onboarding.getStarted') : t('onboarding.next')}
          </button>
        </footer>
      </section>
    </>
  );
}

function OnboardingFeature({
  icon,
  title,
  text,
}: {
  icon: string;
  title: string;
  text: string;
}) {
  return (
    <div>
      {materialIcon(icon)}
      <strong>{title}</strong>
      <p>{text}</p>
    </div>
  );
}

type RunStepTrace = {
  id: string;
  type: string;
  status: string;
  title: string;
  detail: string;
  payload: Record<string, unknown>;
  created_at: string;
};

type RunTrace = {
  id: string;
  agent_id: string;
  agent_name: string;
  task_id: string | null;
  status: string;
  provider: string;
  model: string;
  error: string;
  created_at: string;
  completed_at: string | null;
  waiting_on: string | null;
  steps: RunStepTrace[];
};

const RUN_STEP_ICON: Record<string, string> = {
  message: 'chat_bubble',
  thinking: 'psychology',
  tool_call: 'bolt',
  tool_result: 'task_alt',
  approval_required: 'gpp_maybe',
  status: 'info',
  final: 'flag',
};

// Audit/timeline view — surfaces the run/run_steps trace that already gets
// written on every run but had zero UI before this (see CHANGELOG: multiple
// early users independently asked for exactly this on Product Hunt).
function RunTraceModal({
  conversationId,
  token,
  agents,
  onClose,
}: {
  conversationId: string;
  token: string | null;
  agents: Agent[];
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [runs, setRuns] = useState<RunTrace[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setRuns(null);
    setError(null);
    apiRequest<RunTrace[]>(`/conversations/${conversationId}/runs`, { token: token ?? undefined })
      .then((data) => {
        if (!cancelled) setRuns(data);
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, [conversationId, token]);

  const agentById = (id: string) => agents.find((agent) => agent.id === id);

  return (
    <Modal
      title={t('runTrace.title')}
      description={t('runTrace.description')}
      width={720}
      onClose={onClose}
    >
      {error && <EmptyState>{error}</EmptyState>}
      {!error && runs === null && <EmptyState>{t('runTrace.loading')}</EmptyState>}
      {!error && runs !== null && runs.length === 0 && (
        <EmptyState>{t('runTrace.empty')}</EmptyState>
      )}
      {!error && runs !== null && runs.length > 0 && (
        <div className="run-trace-list">
          {runs.map((run) => {
            const agent = agentById(run.agent_id);
            return (
              <div className="run-trace-card" key={run.id}>
                <header className="run-trace-card-header">
                  <div
                    className="run-trace-avatar"
                    style={{ background: agent ? avatarColor(agent) : '#94a3b8' }}
                  >
                    {avatarText(run.agent_name)}
                  </div>
                  <div className="run-trace-card-meta">
                    <strong>{run.agent_name}</strong>
                    <span>
                      {run.provider} · {run.model || '—'} · {formatTime(run.created_at)}
                    </span>
                  </div>
                  <span className={`run-trace-status run-trace-status-${run.status}`}>
                    {t(`runTrace.status.${run.status}`, { defaultValue: run.status })}
                  </span>
                </header>
                {run.waiting_on && (
                  <p className="run-trace-waiting-on">
                    {materialIcon('hourglass_empty')}
                    {run.waiting_on}
                  </p>
                )}
                {run.error && <p className="run-trace-error">{run.error}</p>}
                <ol className="run-trace-steps">
                  {run.steps.map((step) => (
                    <li key={step.id}>
                      <span className="run-trace-step-icon">
                        {materialIcon(RUN_STEP_ICON[step.type] ?? 'circle')}
                      </span>
                      <div className="run-trace-step-body">
                        <strong>{step.title || step.type}</strong>
                        {step.detail && <p>{step.detail}</p>}
                        {Object.keys(step.payload).length > 0 && (
                          <pre className="run-trace-step-payload">
                            {JSON.stringify(step.payload, null, 2)}
                          </pre>
                        )}
                      </div>
                      <time>{formatTime(step.created_at)}</time>
                    </li>
                  ))}
                  {run.steps.length === 0 && (
                    <li className="run-trace-step-empty">{t('runTrace.noSteps')}</li>
                  )}
                </ol>
              </div>
            );
          })}
        </div>
      )}
    </Modal>
  );
}

function Modal({
  title,
  description,
  width,
  onClose,
  children,
}: {
  title: string;
  description: string;
  width: number;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <>
      <div className="overlay blur" onClick={onClose} />
      <section className="modal" style={{ width }} aria-label={title}>
        <header className="modal-header">
          <h2>{title}</h2>
          <p>{description}</p>
        </header>
        <div className="modal-body">{children}</div>
      </section>
    </>
  );
}

function FieldLabel({ children }: { children: ReactNode }) {
  return <div className="field-label">{children}</div>;
}

function ChipList({
  items,
  emptyText,
  muted = false,
}: {
  items: string[];
  emptyText: string;
  muted?: boolean;
}) {
  if (!items.length) return <EmptyState>{emptyText}</EmptyState>;

  return (
    <div className="chip-list">
      {items.map((item) => (
        <span className={muted ? 'chip muted' : 'chip'} key={item}>
          {muted ? materialIcon('extension') : materialIcon('bolt')}
          {item}
        </span>
      ))}
    </div>
  );
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
