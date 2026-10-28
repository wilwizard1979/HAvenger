import { getBase } from './api';

export type ProjectStatus = 'idea' | 'planned' | 'in_progress' | 'on_hold' | 'blocked' | 'done';

export interface Project {
  id: string;
  title: string;
  area: string;
  status: ProjectStatus;
  status_text: string;
  added: string | null;
  summary: string;
  plan: string[];
  next: string;
  notes: string;
  user_tasks: string[];
  source: string;
}

export interface ProjectsOverview {
  projects: Project[];
  counts: Record<ProjectStatus, number>;
  areas: string[];
  needs_you: { text: string; project: string }[];
  janitor_backlog: { group: string; items: string[] }[];
  files: { name: string; title: string; purpose: string; modified: number }[];
  dir: string;
}

export async function fetchProjects(): Promise<ProjectsOverview> {
  const res = await fetch(`${getBase()}/v1/projects`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as ProjectsOverview;
}
