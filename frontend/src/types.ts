export type Status = 'online' | 'offline' | 'unknown';
export interface AuthSession { auth_enabled: boolean; authenticated: boolean; role: 'admin' | 'guest' | null; username: string | null; must_change_password: boolean }
export type Group = 'computadores' | 'consoles' | 'impressoras_3d' | 'infraestrutura' | 'celular' | 'visitantes' | 'sem_grupo';
export const groups: Record<Group, string> = { computadores: 'Computadores', consoles: 'Consoles', impressoras_3d: 'Impressoras 3D', infraestrutura: 'Infraestrutura', celular: 'Celular', visitantes: 'Visitantes', sem_grupo: 'Sem grupo' };
export const groupIcons: Record<Group, string> = { computadores: 'computer', consoles: 'gamepad', impressoras_3d: 'printer3d', infraestrutura: 'router', celular: 'smartphone', visitantes: 'visitors', sem_grupo: 'unknownDevice' };
export interface Device {
  id: number; ip: string | null; mac: string | null; hostname: string | null; name: string | null;
  display_name: string; auto_name: string | null; auto_group: Group; vendor: string | null;
  identification_source: string | null; identified_at: string | null; group_customized: boolean;
  notes: string | null; group: Group; status: Status; last_seen: string | null;
  last_checked: string | null; rtt_ms: number | null; failure_streak: number; success_streak: number;
  first_failed_at: string | null; ever_online: boolean; created_at: string;
  services_checked_at: string | null; services_error: string | null;
  mac_address_type: 'global' | 'private' | 'multicast' | 'unknown';
}
export interface DeviceAvailability {
  samples: number; successes: number; loss_pct: number | null;
  avg_rtt_ms: number | null; min_rtt_ms: number | null; max_rtt_ms: number | null;
}
export interface DeviceService {
  transport: 'tcp'; port: number; service: string; latency_ms: number | null;
  http_status: number | null; server: string | null; content_type: string | null; observed_at: string;
}
export interface DeviceDetails extends Device {
  services: DeviceService[];
  router: RouterClientInfo | null;
  availability: Record<'1h' | '24h' | '7d', DeviceAvailability>;
  recent_incidents: Incident[];
  checked_tcp_ports: number;
}
export interface RouterClientInfo {
  router_online: boolean; interface: 'wired' | 'wifi_2_4' | 'wifi_5' | 'unknown';
  ssid: string | null; rssi_dbm: number | null; phy_mode: string | null;
  power_save: boolean | null; short_guard_interval: boolean | null; stbc: boolean | null;
  mu_beamforming: boolean | null; spatial_streams: number | null; channel_width_mhz: number | null;
  tx_rate_mbps: number | null; rx_rate_mbps: number | null; connected_seconds: number | null;
  ip_method: string | null; dhcp_expires_seconds: number | null; internet_allowed: boolean | null;
  router_device_type: number | null; observed_at: string;
}
export interface RouterBand {
  band: string; ssid: string; channel: number | null; channel_width_mhz: number | null;
  noise_dbm: number | null; utilization_pct: number | null; interference: string | null;
}
export interface RouterStatus {
  available: boolean; host?: string; manufacturer?: string; model?: string | null; firmware?: string | null;
  latency_ms?: number | null; observed_at?: string; error?: string | null;
  cpu_memory?: { cpu_percent: number[]; memory_total_mb: number | null; memory_used_mb: number | null; memory_free_mb: number | null; memory_used_pct: number | null };
  bands?: RouterBand[]; client_counts?: { online: number; known: number; wired: number; wifi_2_4: number; wifi_5: number };
  traffic_rates?: Record<string, { rx_mbps: number; tx_mbps: number }>;
}
export interface RouterClient extends RouterClientInfo {
  device_id: number; display_name: string; ip: string | null; mac: string | null; status: Status;
}
export interface RouterWifiEvent {
  timestamp: string; device_id: number | null; display_name: string; mac: string;
  event: 'authentication' | 'associated' | 'reassociated' | 'disconnected' | 'deauthenticated';
  reason: string | null; rssi_dbm: number | null;
}
export interface RouterWifiAlert {
  device_id: number | null; display_name: string; mac: string; event_count: number;
  last_event_at: string; min_rssi_dbm: number | null; max_rssi_dbm: number | null;
}
export interface RouterDashboard {
  router: RouterStatus; clients: RouterClient[]; wifi_events: RouterWifiEvent[]; wifi_alerts: RouterWifiAlert[];
}
export interface Target { id: number; name: string; host: string; scope: 'local' | 'external'; kind: 'icmp' | 'dns' | 'tcp'; port: number | null; query: string | null; enabled: boolean; status: Status; last_seen: string | null; last_checked: string | null; rtt_ms: number | null; loss_pct: number | null; samples_count: number }
export interface Sample { timestamp: string; success: boolean; rtt_ms: number | null }
export interface Incident { id: number; entity_type: string; entity_id: number; name: string; started_at: string; recovered_at: string | null; duration_seconds: number | null; ongoing: boolean }
export interface Summary { network: { label: string; cidr: string; interface: string; ssids: string[] }; collector: { last_seen: string | null; stale: boolean; error?: string | null }; counts: { devices: number; online: number; offline: number; unknown: number; open_incidents: number }; diagnosis: { level: string; title: string; detail: string }[]; last_discovery: string | null; discovery_running?: boolean }
export interface Settings { label: string; cidr: string; interface: string; ssids: string[]; interval_seconds: number; discovery_interval_seconds: number; retention_days: number; failure_threshold: number; recovery_threshold: number }
export interface DashboardData { summary: Summary; devices: Device[]; targets: Target[]; incidents: Incident[]; settings: Settings }
export type PrinterState = 'IDLE' | 'PRINTING' | 'PAUSED' | 'COMPLETED' | 'ERROR' | 'OFFLINE' | 'UNKNOWN';
export interface PrinterTemperature { current: number | null; target: number | null }
export interface PrinterComponentHealth {
  status: string; available: boolean | null; latency_ms: number | null; message: string | null;
}
export interface Printer {
  id: number; device_id: number | null; name: string; manufacturer: string; model: string; adapter_type: string;
  enabled: boolean; camera_enabled: boolean; online: boolean; connection_status: string; state: PrinterState;
  progress: number | null; current_file: string | null; remaining_seconds: number | null;
  temperatures: { nozzle: PrinterTemperature; bed: PrinterTemperature }; last_seen: string | null;
  capabilities: Record<string, boolean>;
  camera: { available: boolean; reason: string | null; protocol: string | null; stream_url: string | null };
  latency_ms: number | null;
  health: { overall: string; components: Record<string, PrinterComponentHealth> };
  ip: string | null; mac: string | null; network_status: Status | null; network_last_seen: string | null; error: string | null;
}
export interface PrinterDetailsResponse { id: number; details: Record<string, unknown> }
export interface PrinterFile {
  path: string; name: string; size: number | null; modified?: string | null; modified_at_epoch?: number | null;
  print_ready: boolean; preview_available: boolean; metadata: Record<string, unknown>;
}
export interface PrinterPlate {
  index: number; path: string; bed_type?: string | null; filament_colors?: string[]; filament_ids?: number[];
  nozzle_diameter?: number | null; estimated_seconds?: number | null; filament_weight_g?: number | null;
  preview_available?: boolean;
}
export interface PrinterFileInspection {
  id: number; file: PrinterFile; plates: PrinterPlate[]; print_options: Record<string, boolean>;
}
export interface PrinterHistoryEntry {
  id: number | string | null; file_name: string | null; started_at: string | null;
  duration_seconds: number | null; filament_mm: number | null; completed: boolean; size: number | null;
}
export interface SpeedtestConfig {
  id: number; enabled: boolean; interval_minutes: number; download_min_mbps: number | null;
  upload_min_mbps: number | null; failure_threshold: number; recovery_threshold: number;
  retention_days: number; next_run_at: string | null; updated_at: string;
}
export interface SpeedtestResult {
  id: number; job_id: number | null; trigger: 'manual' | 'scheduled'; success: number;
  started_at: string; completed_at: string; duration_seconds: number;
  download_mbps: number | null; upload_mbps: number | null; ping_ms: number | null;
  jitter_ms: number | null; packet_loss_pct: number | null; download_bytes: number | null;
  upload_bytes: number | null; total_bytes: number | null; server_id: string | null;
  server_name: string | null; server_location: string | null; server_country: string | null;
  isp: string | null; error: string | null;
}
export interface SpeedtestIncident {
  id: number; metric: 'availability' | 'download' | 'upload'; name: string;
  threshold_mbps: number | null; started_at: string; recovered_at: string | null;
  ended_at: string | null; end_reason: string | null; duration_seconds: number; ongoing: boolean;
}
export interface SpeedtestDashboard {
  scope: 'internet'; provider: 'Ookla'; period_days: 1 | 7 | 30; config: SpeedtestConfig;
  active_job: { id: number; trigger: string; status: 'queued' | 'running'; requested_at: string; started_at: string | null } | null;
  last_result: SpeedtestResult | null; history: SpeedtestResult[]; incidents: SpeedtestIncident[];
  month_total_bytes: number;
}
