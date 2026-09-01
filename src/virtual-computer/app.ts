import { App } from "@modelcontextprotocol/ext-apps";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import RFB from "@novnc/novnc";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { RoundedBoxGeometry } from "three/examples/jsm/geometries/RoundedBoxGeometry.js";

type ToolArguments = Record<string, unknown>;
type Operation = "idle" | "terminal_execute" | "read_file" | "write_file" | "edit_file";

interface ComputerPayload {
  operation?: Operation;
  computer_id?: string;
  desktop_environment?: boolean;
  desktop_url?: string | null;
  network_access?: boolean;
  workspace_directory?: string;
  path?: string;
  content?: string;
  old_text?: string;
  new_text?: string;
  stdout?: string;
  stderr?: string;
  exit_code?: number;
  exec_duration_ms?: number;
  size_bytes?: number;
  replacements?: number;
  error?: string;
  computer_attached?: boolean;
  runtime_state?: "pending" | "working" | "ready" | "failed";
  runtime_phase?: string;
  runtime_message?: string;
  runtime_progress?: number;
  runtime_total?: number;
  downloaded_bytes?: number | null;
  download_total_bytes?: number | null;
  runtime_error?: string | null;
}

interface ActivityEvent {
  revision: number;
  phase: "request" | "result";
  operation: Operation;
  arguments: ToolArguments;
  payload?: ComputerPayload;
}

interface DirectoryEntryPayload {
  name: string;
  path: string;
  kind: "directory" | "file" | "other";
  size_bytes: number;
}

interface DirectoryPayload {
  path?: string;
  entries?: DirectoryEntryPayload[];
  error?: string;
}

interface VirtualState {
  computerId: string;
  workspace: string;
  operation: Operation;
  arguments: ToolArguments;
  payload?: ComputerPayload;
  startedAt: number;
  finishedAt?: number;
  previousSurface: "desktop" | "terminal" | "file";
  desktopEnvironment: boolean;
  desktopUrl?: string;
  networkAccess: boolean;
  vncConnected: boolean;
  computerAttached: boolean;
  runtimeState: "pending" | "working" | "ready" | "failed";
  runtimePhase: string;
  runtimeMessage: string;
  runtimeProgress: number;
  runtimeTotal: number;
  downloadedBytes?: number;
  downloadTotalBytes?: number;
  runtimeError?: string;
}

interface KeyDefinition {
  label: string;
  code: string;
  width?: number;
}

interface KeyVisual {
  group: THREE.Group;
  restingY: number;
}

interface ScreenCursor {
  x: number;
  y: number;
  visible: boolean;
  down: boolean;
}

interface ManualState {
  active: boolean;
  surface: "desktop" | "explorer" | "editor" | "terminal";
  currentDirectory: string;
  entries: DirectoryEntryPayload[];
  selectedPath?: string;
  editorPath?: string;
  editorContent: string;
  editorCursor: number;
  editorDirty: boolean;
  terminalInput: string;
  terminalLines: string[];
  scrollOffset: number;
  contextMenu?: { x: number; y: number };
  newFileName?: string;
  loading: boolean;
  error?: string;
  hostConnected: boolean;
}

const KEYSTROKE_MS = 72;
const MAX_ANIMATED_KEYSTROKES = 360;
const VIRTUAL_EDITOR_DELAY_MS = 1120;

const sceneCanvas = document.getElementById("scene") as HTMLCanvasElement;
const vncSource = document.getElementById("vnc-source") as HTMLDivElement;

const monitor = document.createElement("canvas");
monitor.width = 1024;
monitor.height = 640;
const m = monitor.getContext("2d")!;
const monitorTexture = new THREE.CanvasTexture(monitor);
monitorTexture.colorSpace = THREE.SRGBColorSpace;
monitorTexture.anisotropy = 8;

const state: VirtualState = {
  computerId: "Virtual Computer",
  workspace: "/workspace",
  operation: "idle",
  arguments: {},
  startedAt: performance.now(),
  previousSurface: "desktop",
  desktopEnvironment: false,
  networkAccess: true,
  vncConnected: false,
  computerAttached: false,
  runtimeState: "ready",
  runtimePhase: "ready",
  runtimeMessage: "Docker is ready.",
  runtimeProgress: 4,
  runtimeTotal: 4,
};

const manual: ManualState = {
  active: false,
  surface: "desktop",
  currentDirectory: "/workspace",
  entries: [],
  editorContent: "",
  editorCursor: 0,
  editorDirty: false,
  terminalInput: "",
  terminalLines: [],
  scrollOffset: 0,
  loading: false,
  hostConnected: false,
};

const scene = new THREE.Scene();
scene.background = new THREE.Color("#090d13");
scene.fog = new THREE.Fog("#090d13", 8, 22);

const camera = new THREE.PerspectiveCamera(38, 1, 0.05, 100);
camera.position.set(6.8, 5.2, 7.2);

const renderer = new THREE.WebGLRenderer({
  canvas: sceneCanvas,
  antialias: true,
  powerPreference: "high-performance",
});
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.08;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;

const controls = new OrbitControls(camera, sceneCanvas);
controls.target.set(0, 1.25, -0.15);
controls.enableDamping = true;
controls.dampingFactor = 0.065;
controls.minDistance = 5.5;
controls.maxDistance = 12;
controls.maxPolarAngle = Math.PI * 0.47;

scene.add(new THREE.HemisphereLight("#d8e9ff", "#172236", 1.9));
const keyLight = new THREE.DirectionalLight("#fff4df", 4.2);
keyLight.position.set(-4, 8, 5);
keyLight.castShadow = true;
keyLight.shadow.mapSize.set(2048, 2048);
scene.add(keyLight);
const rimLight = new THREE.PointLight("#5fe2c1", 35, 12, 2);
rimLight.position.set(4, 3.8, -3.2);
scene.add(rimLight);

const table = new THREE.Mesh(
  new RoundedBoxGeometry(14, 0.45, 9, 8, 0.18),
  new THREE.MeshStandardMaterial({ color: "#3a261e", roughness: 0.72, metalness: 0.03 }),
);
table.position.y = -0.3;
table.receiveShadow = true;
scene.add(table);

const metal = new THREE.MeshStandardMaterial({ color: "#9ba4ad", roughness: 0.28, metalness: 0.78 });
const darkMetal = new THREE.MeshStandardMaterial({ color: "#171b21", roughness: 0.32, metalness: 0.66 });
const base = new THREE.Mesh(new RoundedBoxGeometry(5.35, 0.22, 3.45, 8, 0.16), metal);
base.position.set(0, 0.02, 0.65);
base.rotation.x = -0.018;
base.castShadow = true;
base.receiveShadow = true;
scene.add(base);

const keyboardRows: KeyDefinition[][] = [
  [
    { label: "Esc", code: "Escape" }, { label: "F1", code: "F1" }, { label: "F2", code: "F2" },
    { label: "F3", code: "F3" }, { label: "F4", code: "F4" }, { label: "F5", code: "F5" },
    { label: "F6", code: "F6" }, { label: "F7", code: "F7" }, { label: "F8", code: "F8" },
    { label: "F9", code: "F9" }, { label: "F10", code: "F10" }, { label: "F11", code: "F11" },
    { label: "F12", code: "F12" }, { label: "Del", code: "Delete" }, { label: "Pwr", code: "Power" },
  ],
  [
    { label: "` ~", code: "`" }, { label: "1 !", code: "1" }, { label: "2 @", code: "2" },
    { label: "3 #", code: "3" }, { label: "4 $", code: "4" }, { label: "5 %", code: "5" },
    { label: "6 ^", code: "6" }, { label: "7 &", code: "7" }, { label: "8 *", code: "8" },
    { label: "9 (", code: "9" }, { label: "0 )", code: "0" }, { label: "- _", code: "-" },
    { label: "= +", code: "=" }, { label: "Backspace", code: "Backspace", width: 2 },
  ],
  [
    { label: "Tab", code: "Tab", width: 1.5 }, { label: "Q", code: "Q" }, { label: "W", code: "W" },
    { label: "E", code: "E" }, { label: "R", code: "R" }, { label: "T", code: "T" },
    { label: "Y", code: "Y" }, { label: "U", code: "U" }, { label: "I", code: "I" },
    { label: "O", code: "O" }, { label: "P", code: "P" }, { label: "[ {", code: "[" },
    { label: "] }", code: "]" }, { label: "\\ |", code: "\\", width: 1.5 },
  ],
  [
    { label: "Caps", code: "CapsLock", width: 1.75 }, { label: "A", code: "A" }, { label: "S", code: "S" },
    { label: "D", code: "D" }, { label: "F", code: "F" }, { label: "G", code: "G" },
    { label: "H", code: "H" }, { label: "J", code: "J" }, { label: "K", code: "K" },
    { label: "L", code: "L" }, { label: "; :", code: ";" }, { label: "' \"", code: "'" },
    { label: "Enter", code: "Enter", width: 2.25 },
  ],
  [
    { label: "Shift", code: "Shift", width: 2.25 }, { label: "Z", code: "Z" }, { label: "X", code: "X" },
    { label: "C", code: "C" }, { label: "V", code: "V" }, { label: "B", code: "B" },
    { label: "N", code: "N" }, { label: "M", code: "M" }, { label: ", <", code: "," },
    { label: ". >", code: "." }, { label: "/ ?", code: "/" }, { label: "Shift", code: "Shift", width: 2.75 },
  ],
  [
    { label: "Ctrl", code: "Control", width: 1.2 }, { label: "Fn", code: "Fn" },
    { label: "Alt", code: "Alt", width: 1.1 }, { label: "Cmd", code: "Meta", width: 1.1 },
    { label: "Space", code: "Space", width: 5 }, { label: "Alt", code: "Alt", width: 1.1 },
    { label: "◀", code: "ArrowLeft" }, { label: "▲", code: "ArrowUp" },
    { label: "▼", code: "ArrowDown" }, { label: "▶", code: "ArrowRight" },
  ],
];

const keyVisuals: KeyVisual[] = [];
const keysByCode = new Map<string, KeyVisual[]>();

function keyLabelMaterial(value: string) {
  const canvas = document.createElement("canvas");
  canvas.width = 256;
  canvas.height = 96;
  const context = canvas.getContext("2d")!;
  context.fillStyle = "#d9e1e8";
  const fontSize = value.length > 7 ? 23 : value.length > 4 ? 29 : value.length > 2 ? 34 : 44;
  context.font = `600 ${fontSize}px Segoe UI, sans-serif`;
  context.textAlign = "center";
  context.textBaseline = "middle";
  context.fillText(value, 128, 50);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.anisotropy = 4;
  return new THREE.MeshBasicMaterial({ map: texture, transparent: true, toneMapped: false, depthWrite: false });
}

const keyUnit = 0.265;
const keyGap = 0.025;
keyboardRows.forEach((row, rowIndex) => {
  const rowUnits = row.reduce((sum, key) => sum + (key.width ?? 1), 0);
  const rowWidth = rowUnits * keyUnit + (row.length - 1) * keyGap;
  let x = -rowWidth / 2;
  row.forEach((definition) => {
    const width = (definition.width ?? 1) * keyUnit;
    const visualWidth = width - keyGap * 0.35;
    const group = new THREE.Group();
    const restingY = 0.172;
    group.position.set(x + width / 2, restingY, -0.57 + rowIndex * 0.245);

    const keycap = new THREE.Mesh(
      new RoundedBoxGeometry(visualWidth, 0.055, 0.19, 3, 0.026),
      darkMetal,
    );
    keycap.castShadow = true;
    group.add(keycap);

    const printedLabel = new THREE.Mesh(
      new THREE.PlaneGeometry(Math.min(visualWidth * 0.82, 0.43), 0.082),
      keyLabelMaterial(definition.label),
    );
    printedLabel.position.y = 0.029;
    printedLabel.rotation.x = -Math.PI / 2;
    printedLabel.renderOrder = 2;
    group.add(printedLabel);

    scene.add(group);
    const visual = { group, restingY };
    keyVisuals.push(visual);
    const entries = keysByCode.get(definition.code) ?? [];
    entries.push(visual);
    keysByCode.set(definition.code, entries);
    x += width + keyGap;
  });
});

const trackpad = new THREE.Mesh(
  new RoundedBoxGeometry(1.75, 0.025, 0.92, 5, 0.07),
  new THREE.MeshStandardMaterial({ color: "#89929b", roughness: 0.35, metalness: 0.5 }),
);
trackpad.position.set(0.45, 0.15, 1.62);
scene.add(trackpad);

const screenGroup = new THREE.Group();
screenGroup.position.set(0, 1.78, -1.0);
screenGroup.rotation.x = -0.12;
scene.add(screenGroup);
const lid = new THREE.Mesh(new RoundedBoxGeometry(5.28, 3.34, 0.2, 8, 0.14), darkMetal);
lid.castShadow = true;
screenGroup.add(lid);
const screen = new THREE.Mesh(
  new THREE.PlaneGeometry(4.88, 3.05),
  new THREE.MeshBasicMaterial({ map: monitorTexture, toneMapped: false }),
);
screen.position.z = 0.108;
screenGroup.add(screen);


function mugSurfaceTexture() {
  const canvas = document.createElement("canvas");
  canvas.width = 2048;
  canvas.height = 512;
  const context = canvas.getContext("2d")!;
  context.fillStyle = "#d7d1c5";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.fillStyle = "#24272b";
  context.font = "700 82px Segoe UI, sans-serif";
  context.textAlign = "center";
  context.textBaseline = "middle";
  context.fillText("I <3 virtual desktops", canvas.width * 0.25, canvas.height * 0.5, 700);
  context.fillText("I <3 real desktops", canvas.width * 0.75, canvas.height * 0.5, 700);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.anisotropy = renderer.capabilities.getMaxAnisotropy();
  return texture;
}

const mugMaterial = new THREE.MeshStandardMaterial({
  map: mugSurfaceTexture(),
  roughness: 0.52,
  side: THREE.DoubleSide,
});
const mug = new THREE.Mesh(
  new THREE.CylinderGeometry(0.47, 0.4, 0.92, 64, 1, true, -Math.PI / 2),
  mugMaterial,
);
mug.position.set(3.75, 0.39, -0.35);
mug.castShadow = true;
scene.add(mug);
const mugFacingRotation = Math.atan2(
  camera.position.x - mug.position.x,
  camera.position.z - mug.position.z,
);
const mugInterior = new THREE.Mesh(
  new THREE.CylinderGeometry(0.425, 0.36, 0.76, 32, 1, true),
  new THREE.MeshStandardMaterial({ color: "#aaa49a", roughness: 0.7, side: THREE.BackSide }),
);
mugInterior.position.y = 0.06;
mug.add(mugInterior);
const mugRim = new THREE.Mesh(
  new THREE.TorusGeometry(0.448, 0.028, 10, 40),
  new THREE.MeshStandardMaterial({ color: "#ece7dc", roughness: 0.42 }),
);
mugRim.position.y = 0.46;
mugRim.rotation.x = -Math.PI / 2;
mug.add(mugRim);
const mugContents = new THREE.Mesh(
  new THREE.CircleGeometry(0.38, 32),
  new THREE.MeshStandardMaterial({ color: "#3d2418", roughness: 0.3, side: THREE.DoubleSide }),
);
mugContents.position.y = 0.34;
mugContents.rotation.x = -Math.PI / 2;
mug.add(mugContents);

const mugHandleCurve = new THREE.CubicBezierCurve3(
  new THREE.Vector3(0.4, 0.26, 0),
  new THREE.Vector3(0.72, 0.27, 0),
  new THREE.Vector3(0.72, -0.25, 0),
  new THREE.Vector3(0.38, -0.25, 0),
);
const mugHandle = new THREE.Mesh(
  new THREE.TubeGeometry(mugHandleCurve, 40, 0.052, 12, false),
  new THREE.MeshStandardMaterial({ color: "#d7d1c5", roughness: 0.52 }),
);
mug.add(mugHandle);

const lanCableGroup = new THREE.Group();
scene.add(lanCableGroup);
const LAN_PLUG_LENGTH = 0.42;
const lanPlug = new THREE.Mesh(
  new RoundedBoxGeometry(LAN_PLUG_LENGTH, 0.16, 0.34, 4, 0.04),
  new THREE.MeshStandardMaterial({ color: "#87939d", roughness: 0.32, metalness: 0.62 }),
);
lanPlug.castShadow = true;
lanCableGroup.add(lanPlug);
const lanLatch = new THREE.Mesh(
  new THREE.BoxGeometry(0.2, 0.04, 0.18),
  new THREE.MeshStandardMaterial({ color: "#dce5eb", roughness: 0.35, metalness: 0.45 }),
);
lanLatch.position.set(0, 0.1, 0);
lanPlug.add(lanLatch);
const lanCableMaterial = new THREE.MeshStandardMaterial({
  color: "#2d8f5d",
  roughness: 0.76,
});
let lanCable = new THREE.Mesh(new THREE.BufferGeometry(), lanCableMaterial);
lanCableGroup.add(lanCable);
const pluggedLanPosition = new THREE.Vector3(-2.82, 0.02, 0.36);
const unpluggedLanPosition = new THREE.Vector3(-3.35, 0.06, 0.65);
const pluggedLanRotation = 0;
const unpluggedLanRotation = 0.42;
const cableTableEnd = new THREE.Vector3(-6.55, -0.02, 3.35);
let displayedNetworkAccess = true;

function lanCableEndpoint() {
  return new THREE.Vector3(-LAN_PLUG_LENGTH / 2, 0, 0)
    .applyQuaternion(lanPlug.quaternion)
    .add(lanPlug.position);
}

function updateLanCableGeometry() {
  const endpoint = lanCableEndpoint();
  const alignedApproach = new THREE.Vector3(-0.42, 0, 0).applyQuaternion(lanPlug.quaternion);
  const curve = new THREE.CatmullRomCurve3([
    cableTableEnd,
    new THREE.Vector3(-5.5, -0.01, 2.9),
    new THREE.Vector3(-4.35, 0.02, 1.7),
    endpoint.clone().add(alignedApproach),
    endpoint,
  ]);
  const oldGeometry = lanCable.geometry;
  lanCable.geometry = new THREE.TubeGeometry(curve, 42, 0.055, 10, false);
  lanCableMaterial.color.set(displayedNetworkAccess ? "#2d8f5d" : "#43505a");
  if (oldGeometry) oldGeometry.dispose();
}

lanPlug.position.copy(pluggedLanPosition);
lanPlug.rotation.y = pluggedLanRotation;
updateLanCableGeometry();

const mouseRest = new THREE.Vector3(3.55, 0.055, 1.35);
const mouseGroup = new THREE.Group();
mouseGroup.position.copy(mouseRest);
mouseGroup.rotation.y = -0.16;
scene.add(mouseGroup);

const mouseShellMaterial = new THREE.MeshStandardMaterial({
  color: "#242b33",
  roughness: 0.38,
  metalness: 0.28,
});
const mouseBody = new THREE.Mesh(
  new RoundedBoxGeometry(0.72, 0.2, 1.02, 8, 0.16),
  mouseShellMaterial,
);
mouseBody.castShadow = true;
mouseGroup.add(mouseBody);

const mouseButtonMaterial = new THREE.MeshStandardMaterial({
  color: "#303943",
  roughness: 0.3,
  metalness: 0.34,
});
const leftMouseButton = new THREE.Mesh(
  new RoundedBoxGeometry(0.29, 0.035, 0.39, 5, 0.045),
  mouseButtonMaterial,
);
leftMouseButton.position.set(-0.17, 0.112, -0.26);
leftMouseButton.castShadow = true;
mouseGroup.add(leftMouseButton);
const rightMouseButton = leftMouseButton.clone();
rightMouseButton.position.x = 0.17;
mouseGroup.add(rightMouseButton);

const mouseWheel = new THREE.Mesh(
  new THREE.CylinderGeometry(0.055, 0.055, 0.12, 16),
  new THREE.MeshStandardMaterial({ color: "#11161c", roughness: 0.82 }),
);
mouseWheel.rotation.z = Math.PI / 2;
mouseWheel.position.set(0, 0.145, -0.16);
mouseGroup.add(mouseWheel);

const screenCursor: ScreenCursor = {
  x: 512,
  y: 320,
  visible: true,
  down: false,
};
const raycaster = new THREE.Raycaster();
const pointerNdc = new THREE.Vector2();
const livePressedCodes = new Set<string>();
let pointerOverScreen = false;
let pointerButtons = 0;
let manualToolCallInFlight = false;
let suppressActivityUntil = 0;
let lastScreenPoint: { x: number; y: number } | undefined;
let runtimeSwitchInFlight = false;

function roundedRect(x: number, y: number, width: number, height: number, radius: number) {
  m.beginPath();
  m.roundRect(x, y, width, height, radius);
}

function label(value: string, x: number, y: number, color = "#f5f7fb", font = "18px Segoe UI") {
  m.fillStyle = color;
  m.font = font;
  m.fillText(value, x, y);
}

function clippedLabel(value: string, x: number, y: number, maxWidth: number, color?: string, font?: string) {
  const original = value;
  let clipped = original;
  m.font = font ?? "18px Segoe UI";
  while (clipped.length > 3 && m.measureText(clipped).width > maxWidth) clipped = `${clipped.slice(0, -2)}…`;
  label(clipped, x, y, color, font);
}

function writeAnimationContent() {
  const value = state.payload?.content ?? state.arguments.content ?? "";
  return String(value).replaceAll("\r\n", "\n");
}

function typingDelayMs() {
  if (!state.desktopEnvironment) return VIRTUAL_EDITOR_DELAY_MS;
  return 2750 + (state.previousSurface === "terminal" ? 550 : 0);
}

function keyCodesForCharacter(character: string): string[] {
  if (character === " ") return ["Space"];
  if (character === "\n" || character === "\r") return ["Enter"];
  if (character === "\t") return ["Tab"];

  if (/^[a-z]$/.test(character)) return [character.toUpperCase()];
  if (/^[A-Z]$/.test(character)) return ["Shift", character];
  if (/^[0-9]$/.test(character)) return [character];

  const shiftedSymbols: Record<string, string> = {
    "~": "`", "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6",
    "&": "7", "*": "8", "(": "9", ")": "0", "_": "-", "+": "=", "{": "[",
    "}": "]", "|": "\\", ":": ";", "\"": "'", "<": ",", ">": ".", "?": "/",
  };
  if (character in shiftedSymbols) return ["Shift", shiftedSymbols[character]];
  if (["`", "-", "=", "[", "]", "\\", ";", "'", ",", ".", "/"].includes(character)) {
    return [character];
  }
  return [];
}

function updateKeyboard() {
  keyVisuals.forEach((visual) => {
    visual.group.position.y = visual.restingY;
  });
  if (livePressedCodes.size > 0) {
    livePressedCodes.forEach((code) => {
      const visual = keysByCode.get(code)?.[0];
      if (visual) visual.group.position.y = visual.restingY - 0.043;
    });
    return;
  }
  if (state.operation !== "write_file") return;

  const elapsed = performance.now() - state.startedAt - typingDelayMs();
  const content = writeAnimationContent();
  const count = Math.min(content.length, MAX_ANIMATED_KEYSTROKES);
  if (elapsed < 0 || elapsed >= count * KEYSTROKE_MS) return;

  const characterIndex = Math.floor(elapsed / KEYSTROKE_MS);
  const phase = (elapsed % KEYSTROKE_MS) / KEYSTROKE_MS;
  const depression = Math.sin(Math.PI * phase) * 0.043;
  keyCodesForCharacter(content[characterIndex]).forEach((code) => {
    const visual = keysByCode.get(code)?.[0];
    if (visual) visual.group.position.y = visual.restingY - depression;
  });
}

function updateScreenCursor() {
  const elapsed = performance.now() - state.startedAt;
  screenCursor.visible = true;
  screenCursor.down = false;

  if (state.runtimeState !== "ready") {
    screenCursor.visible = false;
    return;
  }

  if (pointerOverScreen) {
    screenCursor.down = (pointerButtons & 1) !== 0;
    return;
  }

  if (state.operation === "idle") {
    screenCursor.x = 512;
    screenCursor.y = 320;
    return;
  }
  if (state.operation === "terminal_execute") {
    screenCursor.visible = false;
    screenCursor.x = 512;
    screenCursor.y = 320;
    return;
  }
  if (state.previousSurface === "terminal" && elapsed < 430) {
    screenCursor.visible = false;
    return;
  }

  if (elapsed < VIRTUAL_EDITOR_DELAY_MS) {
    const explorerStart = state.previousSurface === "terminal" ? 430 : 0;
    const localElapsed = Math.max(0, elapsed - explorerStart);
    const explorerDuration = VIRTUAL_EDITOR_DELAY_MS - explorerStart;
    const progress = THREE.MathUtils.clamp(localElapsed / explorerDuration, 0, 1);
    if (progress < 0.72) {
      const pathProgress = progress / 0.72;
      screenCursor.x = THREE.MathUtils.lerp(124, 850, pathProgress);
      screenCursor.y = 132;
    } else {
      const fileProgress = (progress - 0.72) / 0.28;
      screenCursor.x = THREE.MathUtils.lerp(850, 480, fileProgress);
      screenCursor.y = THREE.MathUtils.lerp(132, 244, fileProgress);
      screenCursor.down = fileProgress > 0.72 && Math.floor(fileProgress * 18) % 2 === 0;
    }
    return;
  }

  const editorElapsed = elapsed - VIRTUAL_EDITOR_DELAY_MS;
  if (state.operation === "read_file") {
    const scrollProgress = (editorElapsed % 2800) / 2800;
    screenCursor.x = 934;
    screenCursor.y = THREE.MathUtils.lerp(156, 520, scrollProgress);
    return;
  }
  if (state.operation === "edit_file") {
    const selection = String(state.arguments.old_text ?? state.arguments.oldText ?? "");
    const selectionWidth = Math.min(520, Math.max(55, selection.length * 8));
    const dragProgress = THREE.MathUtils.clamp(editorElapsed / 560, 0, 1);
    screenCursor.x = 132 + selectionWidth * dragProgress;
    screenCursor.y = 137;
    screenCursor.down = dragProgress < 0.94;
    return;
  }

  screenCursor.x = 150;
  screenCursor.y = 138;
}

function drawScreenCursor() {
  if (!screenCursor.visible) return;
  m.save();
  m.translate(screenCursor.x, screenCursor.y);
  const scale = screenCursor.down ? 0.9 : 1;
  m.scale(scale, scale);
  m.shadowColor = "rgba(0,0,0,.48)";
  m.shadowBlur = 5;
  m.fillStyle = "#ffffff";
  m.strokeStyle = "#17202a";
  m.lineWidth = 2;
  m.beginPath();
  m.moveTo(0, 0);
  m.lineTo(0, 25);
  m.lineTo(7, 19);
  m.lineTo(13, 31);
  m.lineTo(18, 28);
  m.lineTo(12, 17);
  m.lineTo(23, 16);
  m.closePath();
  m.fill();
  m.stroke();
  m.restore();
}

function updateDeskMouse() {
  const normalizedX = screenCursor.visible ? screenCursor.x / monitor.width - 0.5 : 0;
  const normalizedY = screenCursor.visible ? screenCursor.y / monitor.height - 0.5 : 0;
  const targetX = mouseRest.x + normalizedX * 0.42;
  const targetZ = mouseRest.z + normalizedY * 0.5;
  mouseGroup.position.x = THREE.MathUtils.lerp(mouseGroup.position.x, targetX, 0.16);
  mouseGroup.position.z = THREE.MathUtils.lerp(mouseGroup.position.z, targetZ, 0.16);
  mouseGroup.rotation.y = THREE.MathUtils.lerp(mouseGroup.rotation.y, -0.16 + normalizedX * 0.18, 0.12);
  leftMouseButton.position.y = THREE.MathUtils.lerp(
    leftMouseButton.position.y,
    screenCursor.down ? 0.094 : 0.112,
    0.32,
  );
  if (state.operation === "read_file" && performance.now() - state.startedAt > VIRTUAL_EDITOR_DELAY_MS) {
    mouseWheel.rotation.x += 0.12;
  }
}

function updatePhysicalToggles() {
  const mugTarget = mugFacingRotation + (state.desktopEnvironment ? Math.PI : 0);
  mug.rotation.y = THREE.MathUtils.lerp(mug.rotation.y, mugTarget, 0.12);

  displayedNetworkAccess = state.networkAccess;
  const plugTarget = state.networkAccess ? pluggedLanPosition : unpluggedLanPosition;
  const rotationTarget = state.networkAccess ? pluggedLanRotation : unpluggedLanRotation;
  const previousPosition = lanPlug.position.clone();
  const previousRotation = lanPlug.rotation.y;
  lanPlug.position.lerp(plugTarget, 0.14);
  lanPlug.rotation.y = THREE.MathUtils.lerp(lanPlug.rotation.y, rotationTarget, 0.14);
  if (
    previousPosition.distanceToSquared(lanPlug.position) > 0.0000001
    || Math.abs(previousRotation - lanPlug.rotation.y) > 0.0001
  ) {
    updateLanCableGeometry();
  } else {
    lanCableMaterial.color.set(state.networkAccess ? "#2d8f5d" : "#43505a");
  }
}

function desktopBackground() {
  const gradient = m.createLinearGradient(0, 0, 1024, 640);
  gradient.addColorStop(0, "#16415b");
  gradient.addColorStop(0.52, "#153048");
  gradient.addColorStop(1, "#112036");
  m.fillStyle = gradient;
  m.fillRect(0, 0, 1024, 640);

  m.save();
  m.globalAlpha = 0.34;
  m.fillStyle = "#38d7b4";
  m.beginPath();
  m.arc(840, 500, 315, 0, Math.PI * 2);
  m.fill();
  m.globalAlpha = 0.17;
  m.fillStyle = "#75b9ff";
  m.beginPath();
  m.arc(650, 190, 240, 0, Math.PI * 2);
  m.fill();
  m.restore();

  m.fillStyle = "rgba(8,15,24,.82)";
  m.fillRect(0, 0, 1024, 34);
  label("Applications", 16, 23, "#eef4fb", "13px Segoe UI Semibold");
  clippedLabel(state.computerId, 420, 23, 270, "#cbd6e2", "12px Segoe UI");
  label(new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }), 949, 23, "#eef4fb", "12px Segoe UI");

  desktopIcon(30, 68, "folder", "Workspace");
  desktopIcon(30, 164, "terminal", "Terminal");
  desktopIcon(30, 260, "file", "Files");

  m.fillStyle = "rgba(7,13,21,.78)";
  roundedRect(386, 574, 252, 52, 14);
  m.fill();
  dockIcon(408, 584, "folder");
  dockIcon(466, 584, "terminal");
  dockIcon(524, 584, "file");
  dockIcon(582, 584, "grid");
}

function desktopIcon(x: number, y: number, kind: "folder" | "terminal" | "file", title: string) {
  m.fillStyle = kind === "terminal" ? "#18212e" : kind === "folder" ? "#68b7ef" : "#f0f3f6";
  roundedRect(x + 10, y, 50, 48, 8);
  m.fill();
  if (kind === "folder") {
    m.fillStyle = "#9bd4f7";
    m.fillRect(x + 15, y - 5, 22, 10);
  } else if (kind === "terminal") {
    label(">_", x + 20, y + 31, "#79e6c6", "17px ui-monospace, Consolas");
  } else {
    m.fillStyle = "#9ca9b8";
    m.fillRect(x + 21, y + 14, 28, 3);
    m.fillRect(x + 21, y + 23, 28, 3);
    m.fillRect(x + 21, y + 32, 20, 3);
  }
  label(title, x + 4, y + 70, "#ffffff", "12px Segoe UI");
}

function dockIcon(x: number, y: number, kind: "folder" | "terminal" | "file" | "grid") {
  m.fillStyle = kind === "folder" ? "#66b9ef" : kind === "terminal" ? "#172130" : kind === "file" ? "#f2f4f7" : "#4b5a6c";
  roundedRect(x, y, 38, 34, 8);
  m.fill();
  if (kind === "terminal") label(">_", x + 8, y + 23, "#74e2c3", "14px ui-monospace, Consolas");
}

function windowFrame(title: string, x = 88, y = 62, width = 854, height = 500) {
  m.shadowColor = "rgba(0,0,0,.42)";
  m.shadowBlur = 28;
  m.fillStyle = "#e8edf2";
  roundedRect(x, y, width, height, 10);
  m.fill();
  m.shadowBlur = 0;
  m.fillStyle = "#2a3542";
  roundedRect(x, y, width, 38, 10);
  m.fill();
  m.fillRect(x, y + 20, width, 18);
  clippedLabel(title, x + 16, y + 25, width - 120, "#f5f7fa", "13px Segoe UI Semibold");
  for (let index = 0; index < 3; index += 1) {
    m.fillStyle = index === 2 ? "#e26d72" : "rgba(255,255,255,.2)";
    m.beginPath();
    m.arc(x + width - 74 + index * 24, y + 19, 6, 0, Math.PI * 2);
    m.fill();
  }
}

function toolPath() {
  return String(state.payload?.path ?? state.arguments.path ?? state.workspace);
}

function fileName(path: string) {
  return path.split("/").filter(Boolean).at(-1) ?? "untitled";
}

function drawAppSwitcher() {
  desktopBackground();
  m.fillStyle = "rgba(8,13,21,.82)";
  roundedRect(337, 220, 350, 188, 18);
  m.fill();
  label("Terminal", 383, 372, "#c6d1dd", "13px Segoe UI");
  label("Files", 575, 372, "#ffffff", "13px Segoe UI Semibold");
  m.strokeStyle = "#71dfc2";
  m.lineWidth = 3;
  roundedRect(535, 246, 104, 104, 12);
  m.stroke();
  dockIcon(405, 276, "terminal");
  dockIcon(568, 276, "folder");
}

function drawExplorer(elapsed: number) {
  desktopBackground();
  windowFrame("Files");
  m.fillStyle = "#f8fafc";
  m.fillRect(88, 100, 854, 462);

  const path = toolPath();
  const segments = path.split("/").filter(Boolean);
  const revealCount = Math.max(1, Math.min(segments.length, Math.floor(elapsed / 150) + 1));
  const revealed = `/${segments.slice(0, revealCount).join("/")}`;
  m.fillStyle = "#e5ebf0";
  roundedRect(108, 113, 814, 36, 6);
  m.fill();
  clippedLabel(revealed, 124, 137, 780, "#344352", "13px ui-monospace, Consolas");

  m.fillStyle = "#eef2f6";
  m.fillRect(108, 169, 814, 34);
  label("Name", 172, 191, "#526170", "12px Segoe UI Semibold");
  label("Type", 760, 191, "#526170", "12px Segoe UI Semibold");

  const target = fileName(path);
  if (revealCount >= segments.length) {
    m.fillStyle = elapsed % 700 > 300 ? "#b7ddeb" : "#dcecf3";
    roundedRect(118, 220, 794, 48, 6);
    m.fill();
    m.fillStyle = "#f8fafc";
    roundedRect(132, 228, 30, 32, 3);
    m.fill();
    clippedLabel(target, 178, 250, 550, "#283746", "14px Segoe UI");
    label("Text document", 760, 250, "#667483", "13px Segoe UI");
  }
}

function drawManualExplorer() {
  desktopBackground();
  windowFrame("Files");
  m.fillStyle = "#f8fafc";
  m.fillRect(88, 100, 854, 462);

  m.fillStyle = "#e5ebf0";
  roundedRect(108, 113, 814, 36, 6);
  m.fill();
  label("↑", 119, 137, "#344352", "18px Segoe UI Semibold");
  clippedLabel(manual.currentDirectory, 154, 137, 750, "#344352", "13px ui-monospace, Consolas");

  m.fillStyle = "#eef2f6";
  m.fillRect(108, 163, 814, 34);
  label("Name", 172, 185, "#526170", "12px Segoe UI Semibold");
  label("Type", 760, 185, "#526170", "12px Segoe UI Semibold");
  label("Size", 866, 185, "#526170", "12px Segoe UI Semibold");

  const visible = manual.entries.slice(manual.scrollOffset, manual.scrollOffset + 8);
  visible.forEach((entry, index) => {
    const y = 207 + index * 42;
    if (manual.selectedPath === entry.path) {
      m.fillStyle = "#b7ddeb";
      roundedRect(118, y, 794, 36, 5);
      m.fill();
    }
    m.fillStyle = entry.kind === "directory" ? "#69b9ef" : entry.kind === "file" ? "#f3f5f7" : "#c4ccd5";
    roundedRect(130, y + 5, 27, 26, 4);
    m.fill();
    if (entry.kind === "directory") {
      m.fillStyle = "#9bd4f7";
      m.fillRect(134, y + 2, 13, 6);
    }
    clippedLabel(entry.name, 172, y + 24, 560, "#283746", "13px Segoe UI");
    label(entry.kind === "directory" ? "Folder" : entry.kind === "file" ? "Text file" : "Other", 760, y + 24, "#667483", "12px Segoe UI");
    if (entry.kind === "file") label(String(entry.size_bytes), 866, y + 24, "#667483", "12px Segoe UI");
  });

  if (manual.loading) label("Loading…", 120, 535, "#637384", "13px Segoe UI");
  else if (manual.error) clippedLabel(manual.error, 120, 535, 790, "#b04850", "13px Segoe UI");
  else if (manual.entries.length === 0) label("This folder is empty", 120, 235, "#738292", "13px Segoe UI");

  if (manual.contextMenu) {
    const { x, y } = manualContextMenuPosition();
    m.shadowColor = "rgba(0,0,0,.35)";
    m.shadowBlur = 18;
    m.fillStyle = "#f7f9fb";
    roundedRect(x, y, 190, 48, 7);
    m.fill();
    m.shadowBlur = 0;
    m.fillStyle = "#e8f1f6";
    roundedRect(x + 5, y + 5, 180, 38, 5);
    m.fill();
    label("＋", x + 16, y + 30, "#2f6e91", "18px Segoe UI Semibold");
    label("New text file", x + 46, y + 30, "#263746", "13px Segoe UI");
  }

  if (manual.newFileName !== undefined) {
    m.fillStyle = "rgba(17,26,36,.28)";
    m.fillRect(88, 100, 854, 462);
    m.shadowColor = "rgba(0,0,0,.4)";
    m.shadowBlur = 24;
    m.fillStyle = "#f7f9fb";
    roundedRect(302, 232, 420, 154, 10);
    m.fill();
    m.shadowBlur = 0;
    label("New text file", 326, 267, "#293746", "15px Segoe UI Semibold");
    m.fillStyle = "#e5ebf0";
    roundedRect(326, 286, 372, 40, 6);
    m.fill();
    clippedLabel(manual.newFileName || "File name", 340, 312, 340, manual.newFileName ? "#253543" : "#8a96a2", "14px Segoe UI");
    label("Enter to open   •   Esc to cancel", 326, 354, "#74818e", "12px Segoe UI");
    if (manual.error) clippedLabel(manual.error, 326, 377, 370, "#b04850", "11px Segoe UI");
  }
}

function drawManualEditor() {
  desktopBackground();
  const path = manual.editorPath ?? "untitled.txt";
  windowFrame(`${fileName(path)}${manual.editorDirty ? " •" : ""} — Text Editor`, 72, 50, 886, 520);
  m.fillStyle = "#f7f8fa";
  m.fillRect(72, 88, 886, 482);
  m.fillStyle = "#e8ecf0";
  m.fillRect(72, 88, 886, 28);
  label("File     Edit     Search     View", 89, 107, "#3a4652", "12px Segoe UI");

  const beforeCursor = manual.editorContent.slice(0, manual.editorCursor).replaceAll("\t", "    ");
  const lines = manual.editorContent.replaceAll("\t", "    ").split("\n");
  const cursorLine = beforeCursor.split("\n").length - 1;
  const firstLine = Math.max(0, cursorLine - 18);
  for (let row = 0; row < 20; row += 1) {
    const lineNumber = firstLine + row;
    if (lineNumber >= lines.length) break;
    label(String(lineNumber + 1).padStart(3), 88, 143 + row * 20, "#9aa5b1", "13px ui-monospace, Consolas");
    clippedLabel(lines[lineNumber], 132, 143 + row * 20, 790, "#263442", "14px ui-monospace, Consolas");
  }
  if (Math.floor(performance.now() / 520) % 2 === 0) {
    const cursorColumn = beforeCursor.split("\n").at(-1) ?? "";
    m.font = "14px ui-monospace, Consolas";
    const cursorX = 132 + Math.min(785, m.measureText(cursorColumn).width);
    const cursorY = 128 + (cursorLine - firstLine) * 20;
    m.fillStyle = "#263442";
    m.fillRect(cursorX, cursorY, 2, 18);
  }
  if (manual.loading) label("Saving…", 872, 548, "#637384", "12px Segoe UI");
  else if (manual.error) clippedLabel(manual.error, 89, 548, 820, "#b04850", "12px Segoe UI");
  else label("Ctrl+S to save", 850, 548, "#7a8794", "11px Segoe UI");
}

function drawManualTerminal() {
  desktopBackground();
  windowFrame("Terminal", 78, 58, 870, 510);
  m.fillStyle = "#101820";
  m.fillRect(78, 96, 870, 472);
  const lines = [...manual.terminalLines, `computer@workstation:${manual.currentDirectory}$ ${manual.terminalInput}`];
  lines.slice(-20).forEach((line, index) => {
    clippedLabel(line, 98, 126 + index * 21, 825, line.includes("$ ") ? "#7ce5c7" : "#d6dee7", "14px ui-monospace, Consolas");
  });
  if (Math.floor(performance.now() / 500) % 2 === 0) {
    const prompt = lines.at(-1) ?? "";
    m.font = "14px ui-monospace, Consolas";
    m.fillStyle = "#d6dee7";
    m.fillRect(98 + Math.min(820, m.measureText(prompt).width), 112 + (Math.min(lines.length, 20) - 1) * 21, 8, 16);
  }
  if (manual.loading) label("Running…", 830, 548, "#81909f", "12px Segoe UI");
  else if (manual.error) clippedLabel(manual.error, 98, 548, 820, "#ed8f95", "12px Segoe UI");
}

function drawManualScreen() {
  if (manual.surface === "desktop") desktopBackground();
  else if (manual.surface === "explorer") drawManualExplorer();
  else if (manual.surface === "editor") drawManualEditor();
  else drawManualTerminal();
}

function editorContent(elapsed: number) {
  const actual = state.payload?.content;
  if (state.operation === "write_file") {
    const content = String(actual ?? state.arguments.content ?? "");
    const animatedCount = Math.min(content.length, MAX_ANIMATED_KEYSTROKES);
    const count = Math.min(animatedCount, Math.floor(Math.max(0, elapsed - VIRTUAL_EDITOR_DELAY_MS) / KEYSTROKE_MS));
    if (content.length > MAX_ANIMATED_KEYSTROKES && count >= animatedCount) return content;
    return content.slice(0, count);
  }
  if (state.operation === "edit_file") {
    if (actual !== undefined) return actual;
    const oldText = String(state.arguments.old_text ?? state.arguments.oldText ?? "");
    const newText = String(state.arguments.new_text ?? state.arguments.newText ?? "");
    return elapsed < 1650 ? oldText : newText;
  }
  return actual ?? "";
}

function drawEditor(elapsed: number) {
  desktopBackground();
  const path = toolPath();
  windowFrame(`${fileName(path)} — Text Editor`, 72, 50, 886, 520);
  m.fillStyle = "#f7f8fa";
  m.fillRect(72, 88, 886, 482);
  m.fillStyle = "#e8ecf0";
  m.fillRect(72, 88, 886, 28);
  label("File     Edit     Search     View", 89, 107, "#3a4652", "12px Segoe UI");

  const content = editorContent(elapsed);
  const lines = content.replaceAll("\t", "    ").split("\n");
  let firstLine = 0;
  if (state.operation === "read_file" && state.payload && lines.length > 20) {
    firstLine = Math.floor((elapsed - 1100) / 850) % Math.max(1, lines.length - 18);
  }
  for (let row = 0; row < 20; row += 1) {
    const lineNumber = firstLine + row;
    if (lineNumber >= lines.length) break;
    label(String(lineNumber + 1).padStart(3), 88, 143 + row * 20, "#9aa5b1", "13px ui-monospace, Consolas");
    clippedLabel(lines[lineNumber], 132, 143 + row * 20, 790, "#263442", "14px ui-monospace, Consolas");
  }

  if (!state.payload && state.operation === "edit_file") {
    const selection = String(state.arguments.old_text ?? state.arguments.oldText ?? "");
    const width = Math.min(520, Math.max(55, selection.length * 8));
    m.fillStyle = elapsed < 1650 ? "rgba(87,156,214,.34)" : "rgba(105,226,194,.22)";
    m.fillRect(129, 127, width, 20);
  }
  if (state.payload && !state.payload.error) {
    m.fillStyle = "#3d9b77";
    m.beginPath();
    m.arc(929, 102, 5, 0, Math.PI * 2);
    m.fill();
  }
}

function drawErrorDialog(message: string) {
  m.fillStyle = "rgba(7,12,19,.38)";
  m.fillRect(0, 34, 1024, 606);
  m.shadowColor = "rgba(0,0,0,.4)";
  m.shadowBlur = 24;
  m.fillStyle = "#f7f8fa";
  roundedRect(287, 224, 450, 172, 10);
  m.fill();
  m.shadowBlur = 0;
  m.fillStyle = "#d4555d";
  m.beginPath();
  m.arc(330, 278, 18, 0, Math.PI * 2);
  m.fill();
  label("!", 325, 285, "#ffffff", "20px Segoe UI Semibold");
  label("File operation", 365, 272, "#293644", "15px Segoe UI Semibold");
  clippedLabel(message, 365, 301, 335, "#546271", "13px Segoe UI");
  m.fillStyle = "#317aae";
  roundedRect(638, 343, 72, 30, 6);
  m.fill();
  label("Close", 657, 364, "#ffffff", "12px Segoe UI Semibold");
}

function drawTerminal() {
  desktopBackground();
  windowFrame("Terminal", 78, 58, 870, 510);
  m.fillStyle = "#101820";
  m.fillRect(78, 96, 870, 472);

  const command = typeof state.arguments.command === "string"
    ? state.arguments.command
    : JSON.stringify(state.arguments.args ?? []);
  const lines = [`computer@workstation:${state.workspace}$ ${command}`];
  if (state.payload) {
    const output = [state.payload.stdout, state.payload.stderr].filter(Boolean).join("\n");
    if (output) lines.push(...output.split("\n"));
    if (state.payload.error) lines.push(state.payload.error);
    lines.push(`computer@workstation:${state.workspace}$`);
  }
  lines.slice(-20).forEach((line, index) => {
    clippedLabel(line, 98, 126 + index * 21, 825, index === 0 ? "#7ce5c7" : "#d6dee7", "14px ui-monospace, Consolas");
  });
  if (!state.payload && Math.floor(performance.now() / 500) % 2 === 0) {
    m.fillStyle = "#d6dee7";
    m.fillRect(99 + Math.min(780, m.measureText(lines[0]).width), 112, 8, 16);
  }
}

function finishExpiredOperation() {
  if (!state.finishedAt) return;
  let animationEnd = state.finishedAt + 6000;
  if (state.operation === "write_file") {
    const keystrokes = Math.min(writeAnimationContent().length, MAX_ANIMATED_KEYSTROKES);
    animationEnd = Math.max(
      animationEnd,
      state.startedAt + typingDelayMs() + keystrokes * KEYSTROKE_MS + 900,
    );
  }
  if (performance.now() <= animationEnd) return;
  state.previousSurface = state.operation === "terminal_execute" ? "terminal" : "file";
  state.operation = "idle";
  state.arguments = {};
  state.payload = undefined;
  state.finishedAt = undefined;
}

function formatBytes(value: number) {
  const units = ["B", "KB", "MB", "GB"];
  let amount = value;
  let unit = units[0];
  for (let index = 1; index < units.length && amount >= 1000; index += 1) {
    amount /= 1000;
    unit = units[index];
  }
  return `${amount >= 100 ? amount.toFixed(0) : amount.toFixed(1)} ${unit}`;
}

function drawDockerSetup() {
  desktopBackground();
  windowFrame("Computer setup", 168, 126, 688, 350);
  m.fillStyle = "#f8fafc";
  m.fillRect(168, 164, 688, 312);

  m.fillStyle = state.runtimeState === "failed" ? "#d4555d" : "#38bda4";
  m.beginPath();
  m.arc(244, 236, 34, 0, Math.PI * 2);
  m.fill();
  label(state.runtimeState === "failed" ? "!" : "D", 231, 248, "#ffffff", "34px Segoe UI Semibold");

  const title = state.runtimeState === "failed"
    ? "Docker setup failed"
    : state.runtimeMessage || "Preparing Docker...";
  label(title, 302, 224, "#233242", "24px Segoe UI Semibold");
  label("Windows host runtime", 303, 252, "#6b7885", "14px Segoe UI");

  if (state.runtimeState === "failed") {
    clippedLabel(state.runtimeError ?? "Docker could not be prepared.", 216, 324, 590, "#a53c48", "14px Segoe UI");
    label("Fix the reported Windows/WSL issue, then retry the tool call.", 216, 361, "#52606d", "13px Segoe UI");
    return;
  }

  const trackX = 216;
  const trackY = 314;
  const trackWidth = 592;
  m.fillStyle = "#dbe3ea";
  roundedRect(trackX, trackY, trackWidth, 18, 9);
  m.fill();

  const hasDownloadTotal = state.runtimePhase === "downloading"
    && state.downloadedBytes !== undefined
    && state.downloadTotalBytes !== undefined
    && state.downloadTotalBytes > 0;
  m.fillStyle = "#2e9f91";
  if (hasDownloadTotal) {
    const ratio = THREE.MathUtils.clamp(state.downloadedBytes! / state.downloadTotalBytes!, 0, 1);
    roundedRect(trackX, trackY, Math.max(18, trackWidth * ratio), 18, 9);
    m.fill();
    label(`${Math.round(ratio * 100)}%`, 768, 363, "#52606d", "13px Segoe UI Semibold");
    label(
      `${formatBytes(state.downloadedBytes!)} / ${formatBytes(state.downloadTotalBytes!)}`,
      216,
      363,
      "#52606d",
      "13px Segoe UI",
    );
  } else {
    const sweepWidth = 132;
    const sweep = ((performance.now() / 8) % (trackWidth + sweepWidth)) - sweepWidth;
    m.save();
    roundedRect(trackX, trackY, trackWidth, 18, 9);
    m.clip();
    roundedRect(trackX + sweep, trackY, sweepWidth, 18, 9);
    m.fill();
    m.restore();
    label("Waiting for Windows...", 216, 363, "#52606d", "13px Segoe UI");
  }

  label("You can leave this open; setup continues if the original tool call times out.", 216, 414, "#6b7885", "13px Segoe UI");
}

function drawVirtualScreen() {
  if (state.runtimeState !== "ready") {
    drawDockerSetup();
    return;
  }
  if (manual.active) {
    drawManualScreen();
    if (pointerOverScreen) drawScreenCursor();
    return;
  }
  if (state.operation === "idle") {
    desktopBackground();
    drawScreenCursor();
    return;
  }
  if (state.operation === "terminal_execute") {
    drawTerminal();
    return;
  }
  const elapsed = performance.now() - state.startedAt;
  if (state.previousSurface === "terminal" && elapsed < 430) drawAppSwitcher();
  else if (elapsed < VIRTUAL_EDITOR_DELAY_MS) drawExplorer(elapsed - (state.previousSurface === "terminal" ? 430 : 0));
  else drawEditor(elapsed);
  if (state.payload?.error) drawErrorDialog(state.payload.error);
  drawScreenCursor();
}

let rfb: RFB | null = null;
let vncCanvas: HTMLCanvasElement | null = null;
let desktopReconnectTimer: number | undefined;
const DESKTOP_AUDIO_SAMPLE_RATE = 48_000;
const DESKTOP_AUDIO_CHANNELS = 2;
let desktopAudioContext: AudioContext | null = null;
let desktopAudioGain: GainNode | null = null;
let desktopAudioSocket: WebSocket | null = null;
let desktopAudioUrl: string | undefined;
let desktopAudioNextStart = 0;
let desktopAudioUnlocked = false;
let desktopAudioReconnectTimer: number | undefined;

function audioUrlForDesktop(vncUrl: string) {
  const audioUrl = new URL(vncUrl);
  audioUrl.pathname = "/audio";
  audioUrl.search = "";
  audioUrl.hash = "";
  return audioUrl.toString();
}

function stopDesktopAudio() {
  if (desktopAudioReconnectTimer !== undefined) {
    window.clearTimeout(desktopAudioReconnectTimer);
    desktopAudioReconnectTimer = undefined;
  }
  const socket = desktopAudioSocket;
  desktopAudioSocket = null;
  desktopAudioUrl = undefined;
  desktopAudioNextStart = 0;
  if (socket) socket.close();
}

function queueDesktopAudio(payload: ArrayBuffer) {
  const context = desktopAudioContext;
  if (!context || context.state !== "running" || payload.byteLength < 4) return;
  const sampleCount = Math.floor(payload.byteLength / 2);
  const frameCount = Math.floor(sampleCount / DESKTOP_AUDIO_CHANNELS);
  if (frameCount === 0) return;

  const samples = new DataView(payload);
  const buffer = context.createBuffer(
    DESKTOP_AUDIO_CHANNELS,
    frameCount,
    DESKTOP_AUDIO_SAMPLE_RATE,
  );
  for (let channel = 0; channel < DESKTOP_AUDIO_CHANNELS; channel += 1) {
    const output = buffer.getChannelData(channel);
    for (let frame = 0; frame < frameCount; frame += 1) {
      const sampleIndex = frame * DESKTOP_AUDIO_CHANNELS + channel;
      output[frame] = samples.getInt16(sampleIndex * 2, true) / 32768;
    }
  }

  const minimumStart = context.currentTime + 0.06;
  if (desktopAudioNextStart < minimumStart || desktopAudioNextStart > context.currentTime + 0.45) {
    desktopAudioNextStart = minimumStart;
  }
  const source = context.createBufferSource();
  source.buffer = buffer;
  source.connect(desktopAudioGain ?? context.destination);
  source.start(desktopAudioNextStart);
  desktopAudioNextStart += buffer.duration;
}

function connectDesktopAudio(vncUrl: string) {
  if (!desktopAudioUnlocked) return;
  const nextUrl = audioUrlForDesktop(vncUrl);
  if (desktopAudioSocket && desktopAudioUrl === nextUrl) return;
  stopDesktopAudio();
  desktopAudioUrl = nextUrl;
  const socket = new WebSocket(nextUrl);
  desktopAudioSocket = socket;
  socket.binaryType = "arraybuffer";
  socket.addEventListener("message", (event) => {
    if (desktopAudioSocket === socket && event.data instanceof ArrayBuffer) {
      queueDesktopAudio(event.data);
    }
  });
  socket.addEventListener("close", () => {
    if (desktopAudioSocket !== socket) return;
    desktopAudioSocket = null;
    desktopAudioUrl = undefined;
    desktopAudioNextStart = 0;
    if (state.desktopEnvironment && state.desktopUrl && desktopAudioUnlocked) {
      desktopAudioReconnectTimer = window.setTimeout(() => {
        desktopAudioReconnectTimer = undefined;
        if (state.desktopUrl) connectDesktopAudio(state.desktopUrl);
      }, 1000);
    }
  });
}

async function unlockDesktopAudio() {
  desktopAudioUnlocked = true;
  if (!desktopAudioContext) {
    desktopAudioContext = new AudioContext({ sampleRate: DESKTOP_AUDIO_SAMPLE_RATE });
    desktopAudioGain = desktopAudioContext.createGain();
    desktopAudioGain.gain.value = 0.82;
    desktopAudioGain.connect(desktopAudioContext.destination);
  }
  if (desktopAudioContext.state !== "running") await desktopAudioContext.resume();
  if (state.desktopEnvironment && state.desktopUrl) connectDesktopAudio(state.desktopUrl);
}

function disconnectDesktop() {
  if (desktopReconnectTimer !== undefined) {
    window.clearTimeout(desktopReconnectTimer);
    desktopReconnectTimer = undefined;
  }
  const connection = rfb;
  rfb = null;
  vncCanvas = null;
  state.vncConnected = false;
  state.desktopUrl = undefined;
  stopDesktopAudio();
  if (connection) connection.disconnect();
  vncSource.replaceChildren();
}

function connectDesktop(url: string) {
  if (rfb && state.desktopUrl === url) return;
  if (rfb) disconnectDesktop();
  state.desktopEnvironment = true;
  state.desktopUrl = url;
  try {
    const connection = new RFB(vncSource, url, { shared: true, credentials: { password: "" } });
    rfb = connection;
    connection.viewOnly = false;
    connection.focusOnClick = true;
    connection.scaleViewport = false;
    connection.resizeSession = false;
    connection.addEventListener("connect", () => {
      if (desktopReconnectTimer !== undefined) {
        window.clearTimeout(desktopReconnectTimer);
        desktopReconnectTimer = undefined;
      }
      state.vncConnected = true;
      vncCanvas = vncSource.querySelector("canvas");
      connectDesktopAudio(url);
    });
    connection.addEventListener("disconnect", () => {
      if (rfb !== connection) return;
      state.vncConnected = false;
      vncCanvas = null;
      rfb = null;
      if (state.desktopEnvironment && state.desktopUrl === url) {
        desktopReconnectTimer = window.setTimeout(() => {
          desktopReconnectTimer = undefined;
          if (!rfb && state.desktopEnvironment && state.desktopUrl === url) {
            connectDesktop(url);
          }
        }, 800);
      }
    });
  } catch {
    rfb = null;
    state.desktopUrl = undefined;
  }
}

function renderMonitor() {
  if (state.vncConnected && vncCanvas) {
    m.fillStyle = "#101820";
    m.fillRect(0, 0, 1024, 640);
    m.drawImage(vncCanvas, 0, 0, 1024, 640);
    if (pointerOverScreen) drawScreenCursor();
  } else {
    drawVirtualScreen();
  }
  monitorTexture.needsUpdate = true;
}

function resize() {
  const width = sceneCanvas.clientWidth;
  const height = sceneCanvas.clientHeight;
  renderer.setSize(width, height, false);
  camera.aspect = width / Math.max(height, 1);
  camera.updateProjectionMatrix();
}

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  finishExpiredOperation();
  updateScreenCursor();
  updateKeyboard();
  updateDeskMouse();
  updatePhysicalToggles();
  renderMonitor();
  renderer.render(scene, camera);
}

function inferOperation(args: ToolArguments): Operation {
  if ("command" in args || "args" in args) return "terminal_execute";
  if ("old_text" in args || "oldText" in args) return "edit_file";
  if ("content" in args) return "write_file";
  if ("path" in args) return "read_file";
  return "idle";
}

function structuredOf<T>(value: unknown): T | undefined {
  if (!value || typeof value !== "object") return undefined;
  const direct = value as { structuredContent?: T; result?: { structuredContent?: T } };
  return direct.structuredContent ?? direct.result?.structuredContent;
}

function payloadOf(value: unknown): ComputerPayload | undefined {
  return structuredOf<ComputerPayload>(value);
}

function applyPayload(payload: ComputerPayload) {
  if (payload.computer_id) state.computerId = payload.computer_id;
  if (payload.workspace_directory) {
    if (manual.currentDirectory === state.workspace) manual.currentDirectory = payload.workspace_directory;
    state.workspace = payload.workspace_directory;
  }
  if (payload.desktop_environment !== undefined) {
    state.desktopEnvironment = payload.desktop_environment;
    if (payload.desktop_environment && payload.desktop_url) connectDesktop(payload.desktop_url);
    else if (!payload.desktop_environment) disconnectDesktop();
  }
  if (payload.network_access !== undefined) state.networkAccess = payload.network_access;
  if (payload.computer_attached !== undefined) state.computerAttached = payload.computer_attached;
  if (payload.runtime_state !== undefined) state.runtimeState = payload.runtime_state;
  if (payload.runtime_phase !== undefined) state.runtimePhase = payload.runtime_phase;
  if (payload.runtime_message !== undefined) state.runtimeMessage = payload.runtime_message;
  if (payload.runtime_progress !== undefined) state.runtimeProgress = payload.runtime_progress;
  if (payload.runtime_total !== undefined) state.runtimeTotal = payload.runtime_total;
  state.downloadedBytes = payload.downloaded_bytes ?? undefined;
  state.downloadTotalBytes = payload.download_total_bytes ?? undefined;
  state.runtimeError = payload.runtime_error ?? undefined;
  if (payload.operation && payload.operation !== "idle") {
    state.operation = payload.operation;
    state.payload = payload;
    state.finishedAt = performance.now();
  }
}

const app = new App(
  { name: "mcp-virtual-computer-view", version: "0.2.2" },
  { availableDisplayModes: ["inline", "pip", "fullscreen"] },
  { autoResize: true },
);
let directClient: Client | null = null;
let runtimePollInFlight = false;
let runtimePollTimer: number | undefined;

async function callManualTool<T>(name: string, args: ToolArguments): Promise<T> {
  if (!manual.hostConnected) throw new Error("Open this computer through an MCP host to use its files and terminal.");
  manualToolCallInFlight = true;
  try {
    const result = directClient
      ? await directClient.callTool({ name, arguments: args })
      : await app.callServerTool({ name, arguments: args });
    const payload = structuredOf<T & { error?: string }>(result);
    if (!payload) throw new Error(`${name} returned no structured result.`);
    if (payload.error) throw new Error(payload.error);
    return payload;
  } finally {
    manualToolCallInFlight = false;
    suppressActivityUntil = performance.now() + 1000;
  }
}

async function pollRuntimeStatus() {
  if (!manual.hostConnected || runtimePollInFlight) return;
  runtimePollInFlight = true;
  try {
    const result = directClient
      ? await directClient.callTool({ name: "runtime_status", arguments: {} })
      : await app.callServerTool({ name: "runtime_status", arguments: {} });
    const payload = payloadOf(result);
    if (!payload) return;
    applyPayload(payload);
    if (payload.runtime_state === "ready" && !payload.computer_attached) {
      const attachResult = directClient
        ? await directClient.callTool({ name: "computer_ui", arguments: {} })
        : await app.callServerTool({ name: "computer_ui", arguments: {} });
      const attached = payloadOf(attachResult);
      if (attached) applyPayload(attached);
    }
  } catch {
    // A cancelled host request does not cancel the shielded server setup task.
  } finally {
    runtimePollInFlight = false;
  }
}

function startRuntimePolling() {
  if (runtimePollTimer !== undefined) return;
  void pollRuntimeStatus();
  runtimePollTimer = window.setInterval(() => void pollRuntimeStatus(), 350);
}

async function toggleNetworkCable() {
  if (runtimeSwitchInFlight) return;
  runtimeSwitchInFlight = true;
  try {
    const payload = await callManualTool<ComputerPayload>(
      "set_network_access",
      { enabled: !state.networkAccess },
    );
    applyPayload(payload);
  } catch (error) {
    console.error("Could not change network access", error);
  } finally {
    runtimeSwitchInFlight = false;
  }
}

async function toggleDesktopMug() {
  if (runtimeSwitchInFlight) return;
  runtimeSwitchInFlight = true;
  try {
    const payload = await callManualTool<ComputerPayload>(
      "set_desktop_environment",
      { enabled: !state.desktopEnvironment },
    );
    applyPayload(payload);
  } catch (error) {
    console.error("Could not switch desktop environment", error);
  } finally {
    runtimeSwitchInFlight = false;
  }
}

async function openManualDirectory(path: string) {
  manual.active = true;
  manual.surface = "explorer";
  manual.loading = true;
  manual.error = undefined;
  manual.selectedPath = undefined;
  try {
    const listing = await callManualTool<DirectoryPayload>("list_directory", { path });
    manual.currentDirectory = listing.path ?? path;
    manual.entries = listing.entries ?? [];
    manual.scrollOffset = 0;
  } catch (error) {
    manual.entries = [];
    manual.error = error instanceof Error ? error.message : String(error);
  } finally {
    manual.loading = false;
  }
}

async function openManualFile(path: string) {
  manual.active = true;
  manual.surface = "editor";
  manual.loading = true;
  manual.error = undefined;
  manual.editorPath = path;
  try {
    const file = await callManualTool<ComputerPayload>("read_file", { path });
    manual.editorPath = file.path ?? path;
    manual.editorContent = file.content ?? "";
    manual.editorCursor = manual.editorContent.length;
    manual.editorDirty = false;
  } catch (error) {
    manual.editorContent = "";
    manual.editorCursor = 0;
    manual.error = error instanceof Error ? error.message : String(error);
  } finally {
    manual.loading = false;
  }
}

async function saveManualFile() {
  if (!manual.editorPath || manual.loading) return;
  manual.loading = true;
  manual.error = undefined;
  try {
    const file = await callManualTool<ComputerPayload>("write_file", {
      path: manual.editorPath,
      content: manual.editorContent,
      create_parent_directories: true,
    });
    manual.editorPath = file.path ?? manual.editorPath;
    manual.editorContent = file.content ?? manual.editorContent;
    manual.editorCursor = Math.min(manual.editorCursor, manual.editorContent.length);
    manual.editorDirty = false;
  } catch (error) {
    manual.error = error instanceof Error ? error.message : String(error);
  } finally {
    manual.loading = false;
  }
}

async function runManualCommand() {
  const command = manual.terminalInput.trim();
  if (!command || manual.loading) return;
  manual.terminalLines.push(`computer@workstation:${manual.currentDirectory}$ ${command}`);
  manual.terminalInput = "";
  manual.loading = true;
  manual.error = undefined;
  try {
    const result = await callManualTool<ComputerPayload>("terminal_execute", {
      command,
      working_directory: manual.currentDirectory,
    });
    const output = [result.stdout, result.stderr].filter(Boolean).join("\n");
    if (output) manual.terminalLines.push(...output.replaceAll("\r\n", "\n").split("\n"));
    if (typeof result.exit_code === "number" && result.exit_code !== 0) {
      manual.terminalLines.push(`[exit ${result.exit_code}]`);
    }
  } catch (error) {
    manual.error = error instanceof Error ? error.message : String(error);
  } finally {
    manual.loading = false;
  }
}

function screenPoint(event: MouseEvent | PointerEvent | WheelEvent) {
  const bounds = sceneCanvas.getBoundingClientRect();
  pointerNdc.set(
    ((event.clientX - bounds.left) / bounds.width) * 2 - 1,
    -((event.clientY - bounds.top) / bounds.height) * 2 + 1,
  );
  raycaster.setFromCamera(pointerNdc, camera);
  const hit = raycaster.intersectObject(screen, false)[0];
  if (!hit?.uv) return undefined;
  return {
    x: THREE.MathUtils.clamp(hit.uv.x * monitor.width, 0, monitor.width - 1),
    y: THREE.MathUtils.clamp((1 - hit.uv.y) * monitor.height, 0, monitor.height - 1),
  };
}

function interactiveSceneObject(event: MouseEvent | PointerEvent) {
  const bounds = sceneCanvas.getBoundingClientRect();
  pointerNdc.set(
    ((event.clientX - bounds.left) / bounds.width) * 2 - 1,
    -((event.clientY - bounds.top) / bounds.height) * 2 + 1,
  );
  raycaster.setFromCamera(pointerNdc, camera);
  const hit = raycaster.intersectObjects([mug, lanCableGroup], true)[0]?.object;
  if (!hit) return undefined;
  let current: THREE.Object3D | null = hit;
  while (current) {
    if (current === mug) return "mug" as const;
    if (current === lanCableGroup) return "lan" as const;
    current = current.parent;
  }
  return undefined;
}

function visibleManualEntryAt(x: number, y: number) {
  if (x < 118 || x > 912 || y < 207 || y >= 543) return undefined;
  const index = manual.scrollOffset + Math.floor((y - 207) / 42);
  return manual.entries[index];
}

function manualContextMenuPosition() {
  return {
    x: THREE.MathUtils.clamp(manual.contextMenu?.x ?? 100, 100, 736),
    y: THREE.MathUtils.clamp(manual.contextMenu?.y ?? 110, 110, 492),
  };
}

function closeButtonAt(point: { x: number; y: number }) {
  const center = manual.surface === "explorer"
    ? { x: 916, y: 81 }
    : manual.surface === "editor"
      ? { x: 932, y: 69 }
      : manual.surface === "terminal"
        ? { x: 922, y: 77 }
        : undefined;
  return center !== undefined && Math.hypot(point.x - center.x, point.y - center.y) <= 14;
}

function closeManualWindow() {
  manual.surface = "desktop";
  manual.contextMenu = undefined;
  manual.newFileName = undefined;
  manual.error = undefined;
}

function openNewFileEditor() {
  const name = manual.newFileName?.trim() ?? "";
  if (!name) {
    manual.error = "Enter a file name.";
    return;
  }
  if (name.includes("/") || name.includes("\\") || name.includes("\0") || name === "." || name === "..") {
    manual.error = "Use a file name without path separators.";
    return;
  }
  manual.editorPath = manual.currentDirectory === "/"
    ? `/${name}`
    : `${manual.currentDirectory.replace(/\/$/, "")}/${name}`;
  manual.editorContent = "";
  manual.editorCursor = 0;
  manual.editorDirty = true;
  manual.newFileName = undefined;
  manual.contextMenu = undefined;
  manual.error = undefined;
  manual.surface = "editor";
}

function parentDirectory(path: string) {
  const segments = path.split("/").filter(Boolean);
  if (segments.length === 0) return "/";
  segments.pop();
  return `/${segments.join("/")}` || "/";
}

function forwardVncMouse(
  type: "mousedown" | "mouseup" | "mousemove",
  point: { x: number; y: number },
  event: MouseEvent | PointerEvent,
) {
  if (!rfb || !vncCanvas) return;
  const bounds = vncCanvas.getBoundingClientRect();
  const width = bounds.width || vncCanvas.width || monitor.width;
  const height = bounds.height || vncCanvas.height || monitor.height;
  const x = point.x / monitor.width * width;
  const y = point.y / monitor.height * height;
  // Synthetic DOM mousedown events make noVNC install a full-viewport capture
  // element whose real mouseup is suppressed by our pointer handler. Inject at
  // noVNC's pointer adapter instead so the next physical click is never blocked.
  const pointer = rfb as unknown as {
    _handleMouseButton(x: number, y: number, mask: number): void;
    _handleMouseMove(x: number, y: number): void;
  };
  if (type === "mousemove") {
    pointer._handleMouseMove(x, y);
    return;
  }
  const buttonMasks = [1, 4, 2, 128, 256];
  const mask = buttonMasks.reduce(
    (value, buttonMask, index) => value | (event.buttons & (1 << index) ? buttonMask : 0),
    0,
  );
  pointer._handleMouseButton(x, y, mask);
}

function forwardVncWheel(point: { x: number; y: number }, event: WheelEvent) {
  if (!vncCanvas) return;
  const bounds = vncCanvas.getBoundingClientRect();
  const width = bounds.width || vncCanvas.width || monitor.width;
  const height = bounds.height || vncCanvas.height || monitor.height;
  vncCanvas.dispatchEvent(new WheelEvent("wheel", {
    bubbles: true,
    cancelable: true,
    clientX: bounds.left + point.x / monitor.width * width,
    clientY: bounds.top + point.y / monitor.height * height,
    deltaX: event.deltaX,
    deltaY: event.deltaY,
    deltaMode: event.deltaMode,
  }));
}

function forwardVncKeyboard(event: KeyboardEvent) {
  if (!vncCanvas) return;
  vncCanvas.dispatchEvent(new KeyboardEvent(event.type, {
    bubbles: true,
    cancelable: true,
    key: event.key,
    code: event.code,
    location: event.location,
    repeat: event.repeat,
    ctrlKey: event.ctrlKey,
    shiftKey: event.shiftKey,
    altKey: event.altKey,
    metaKey: event.metaKey,
  }));
}

function visualCodeForPhysicalKey(code: string) {
  if (code.startsWith("Key")) return code.slice(3);
  if (code.startsWith("Digit")) return code.slice(5);
  const codes: Record<string, string> = {
    Backquote: "`", Minus: "-", Equal: "=", BracketLeft: "[", BracketRight: "]",
    Backslash: "\\", Semicolon: ";", Quote: "'", Comma: ",", Period: ".", Slash: "/",
    Space: "Space", Enter: "Enter", NumpadEnter: "Enter", Tab: "Tab", Backspace: "Backspace",
    Delete: "Delete", Escape: "Escape", ShiftLeft: "Shift", ShiftRight: "Shift",
    ControlLeft: "Control", ControlRight: "Control", AltLeft: "Alt", AltRight: "Alt",
    MetaLeft: "Meta", MetaRight: "Meta", ArrowLeft: "ArrowLeft", ArrowRight: "ArrowRight",
    ArrowUp: "ArrowUp", ArrowDown: "ArrowDown",
  };
  return codes[code] ?? code;
}

function insertEditorText(value: string) {
  manual.editorContent = `${manual.editorContent.slice(0, manual.editorCursor)}${value}${manual.editorContent.slice(manual.editorCursor)}`;
  manual.editorCursor += value.length;
  manual.editorDirty = true;
  manual.error = undefined;
}

function handleManualPointer(point: { x: number; y: number }) {
  if (!manual.active) {
    manual.active = true;
    manual.surface = "desktop";
  }
  if (closeButtonAt(point)) {
    closeManualWindow();
    return;
  }
  if (manual.surface === "explorer") {
    if (manual.newFileName !== undefined) return;
    if (manual.contextMenu) {
      const { x, y } = manualContextMenuPosition();
      if (point.x >= x && point.x <= x + 190 && point.y >= y && point.y <= y + 48) {
        manual.contextMenu = undefined;
        manual.newFileName = "";
        manual.error = undefined;
        return;
      }
      manual.contextMenu = undefined;
    }
    if (point.x >= 108 && point.x <= 150 && point.y >= 112 && point.y <= 151) {
      void openManualDirectory(parentDirectory(manual.currentDirectory));
      return;
    }
    const entry = visibleManualEntryAt(point.x, point.y);
    manual.selectedPath = entry?.path;
  } else if (manual.surface === "editor" && point.x >= 120 && point.y >= 120) {
    manual.editorCursor = manual.editorContent.length;
  }
}

function handleManualDoubleClick(point: { x: number; y: number }) {
  if (!manual.active || manual.surface === "desktop") {
    manual.active = true;
    if (point.x >= 22 && point.x <= 102 && point.y >= 54 && point.y <= 145) {
      void openManualDirectory(state.workspace);
    } else if (point.x >= 22 && point.x <= 102 && point.y >= 150 && point.y <= 245) {
      manual.surface = "terminal";
      manual.error = undefined;
    } else if (point.x >= 22 && point.x <= 102 && point.y >= 246 && point.y <= 345) {
      void openManualDirectory(manual.currentDirectory);
    }
    return;
  }
  if (manual.surface !== "explorer") return;
  if (manual.newFileName !== undefined || manual.contextMenu !== undefined) return;
  const entry = visibleManualEntryAt(point.x, point.y);
  if (!entry) return;
  if (entry.kind === "directory") void openManualDirectory(entry.path);
  else if (entry.kind === "file") void openManualFile(entry.path);
}

function handleVirtualKeyDown(event: KeyboardEvent) {
  if (!manual.active) return false;
  if (manual.surface === "explorer" && manual.newFileName !== undefined) {
    if (event.key === "Escape") {
      manual.newFileName = undefined;
      manual.error = undefined;
      return true;
    }
    if (event.key === "Enter") {
      openNewFileEditor();
      return true;
    }
    if (event.key === "Backspace") {
      manual.newFileName = manual.newFileName.slice(0, -1);
      manual.error = undefined;
      return true;
    }
    if (!event.ctrlKey && !event.metaKey && !event.altKey && event.key.length === 1) {
      manual.newFileName += event.key;
      manual.error = undefined;
      return true;
    }
    return false;
  }
  if (event.key === "Escape") {
    closeManualWindow();
    return true;
  }
  if (manual.surface === "terminal") {
    if (event.ctrlKey && event.key.toLowerCase() === "l") {
      manual.terminalLines = [];
      return true;
    }
    if (event.key === "Enter") {
      void runManualCommand();
      return true;
    }
    if (event.key === "Backspace") {
      manual.terminalInput = manual.terminalInput.slice(0, -1);
      return true;
    }
    if (!event.ctrlKey && !event.metaKey && !event.altKey && event.key.length === 1) {
      manual.terminalInput += event.key;
      return true;
    }
    return false;
  }
  if (manual.surface === "editor") {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      void saveManualFile();
      return true;
    }
    if (event.key === "Backspace") {
      if (manual.editorCursor > 0) {
        manual.editorContent = `${manual.editorContent.slice(0, manual.editorCursor - 1)}${manual.editorContent.slice(manual.editorCursor)}`;
        manual.editorCursor -= 1;
        manual.editorDirty = true;
      }
      return true;
    }
    if (event.key === "Delete") {
      manual.editorContent = `${manual.editorContent.slice(0, manual.editorCursor)}${manual.editorContent.slice(manual.editorCursor + 1)}`;
      manual.editorDirty = true;
      return true;
    }
    if (event.key === "ArrowLeft") {
      manual.editorCursor = Math.max(0, manual.editorCursor - 1);
      return true;
    }
    if (event.key === "ArrowRight") {
      manual.editorCursor = Math.min(manual.editorContent.length, manual.editorCursor + 1);
      return true;
    }
    if (event.key === "Enter") {
      insertEditorText("\n");
      return true;
    }
    if (event.key === "Tab") {
      insertEditorText("\t");
      return true;
    }
    if (!event.ctrlKey && !event.metaKey && !event.altKey && event.key.length === 1) {
      insertEditorText(event.key);
      return true;
    }
    return false;
  }
  if (manual.surface === "explorer") {
    const currentIndex = manual.entries.findIndex((entry) => entry.path === manual.selectedPath);
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      const delta = event.key === "ArrowDown" ? 1 : -1;
      const next = THREE.MathUtils.clamp(currentIndex < 0 ? 0 : currentIndex + delta, 0, Math.max(0, manual.entries.length - 1));
      manual.selectedPath = manual.entries[next]?.path;
      manual.scrollOffset = THREE.MathUtils.clamp(manual.scrollOffset, Math.max(0, next - 7), next);
      return true;
    }
    if (event.key === "Enter" && currentIndex >= 0) {
      const entry = manual.entries[currentIndex];
      if (entry.kind === "directory") void openManualDirectory(entry.path);
      else if (entry.kind === "file") void openManualFile(entry.path);
      return true;
    }
    if (event.key === "Backspace") {
      void openManualDirectory(parentDirectory(manual.currentDirectory));
      return true;
    }
  }
  return false;
}

app.ontoolinput = (params) => {
  if (manualToolCallInFlight) return;
  const args = (params.arguments ?? {}) as ToolArguments;
  const operation = inferOperation(args);
  if (operation === "idle") return;
  manual.active = false;
  state.previousSurface = state.operation === "terminal_execute" ? "terminal" : state.operation === "idle" ? state.previousSurface : "file";
  state.operation = operation;
  state.arguments = args;
  state.payload = undefined;
  state.startedAt = performance.now();
  state.finishedAt = undefined;
};

app.ontoolresult = (params) => {
  if (manualToolCallInFlight) return;
  const payload = payloadOf(params);
  if (payload) applyPayload(payload);
};

window.addEventListener("resize", resize);
window.addEventListener("keydown", (event) => {
  if (state.vncConnected && document.activeElement === vncCanvas) {
    livePressedCodes.add(visualCodeForPhysicalKey(event.code));
  }
}, { capture: true });
window.addEventListener("keyup", (event) => {
  if (state.vncConnected) livePressedCodes.delete(visualCodeForPhysicalKey(event.code));
}, { capture: true });
sceneCanvas.addEventListener("pointermove", (event) => {
  const point = screenPoint(event);
  pointerButtons = event.buttons;
  if (!point) {
    pointerOverScreen = false;
    const interactive = interactiveSceneObject(event);
    sceneCanvas.style.cursor = interactive && !runtimeSwitchInFlight
      ? "pointer"
      : event.buttons
        ? "grabbing"
        : "grab";
    return;
  }
  pointerOverScreen = true;
  lastScreenPoint = point;
  screenCursor.x = point.x;
  screenCursor.y = point.y;
  sceneCanvas.style.cursor = "none";
  if (state.vncConnected) forwardVncMouse("mousemove", point, event);
});

sceneCanvas.addEventListener("pointerdown", (event) => {
  void unlockDesktopAudio();
  const point = screenPoint(event);
  if (!point) {
    const interactive = interactiveSceneObject(event);
    if (interactive === "lan") {
      event.preventDefault();
      void toggleNetworkCable();
    } else if (interactive === "mug") {
      event.preventDefault();
      void toggleDesktopMug();
    }
    return;
  }
  event.preventDefault();
  pointerOverScreen = true;
  pointerButtons = event.buttons;
  lastScreenPoint = point;
  screenCursor.x = point.x;
  screenCursor.y = point.y;
  controls.enabled = false;
  sceneCanvas.setPointerCapture(event.pointerId);
  if (state.vncConnected) {
    rfb?.focus({ preventScroll: true });
    forwardVncMouse("mousedown", point, event);
  } else {
    sceneCanvas.focus({ preventScroll: true });
    handleManualPointer(point);
  }
});

sceneCanvas.addEventListener("pointerup", (event) => {
  const point = screenPoint(event) ?? lastScreenPoint;
  pointerButtons = event.buttons;
  if (point && state.vncConnected) forwardVncMouse("mouseup", point, event);
  controls.enabled = true;
  if (sceneCanvas.hasPointerCapture(event.pointerId)) sceneCanvas.releasePointerCapture(event.pointerId);
});

sceneCanvas.addEventListener("pointerleave", () => {
  if (pointerButtons !== 0) return;
  pointerOverScreen = false;
  controls.enabled = true;
  sceneCanvas.style.cursor = "grab";
});

sceneCanvas.addEventListener("dblclick", (event) => {
  const point = screenPoint(event);
  if (point) {
    event.preventDefault();
    if (!state.vncConnected) handleManualDoubleClick(point);
    return;
  }
  camera.position.set(6.8, 5.2, 7.2);
  controls.target.set(0, 1.25, -0.15);
});

sceneCanvas.addEventListener("wheel", (event) => {
  const point = screenPoint(event);
  if (!point) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  if (state.vncConnected) {
    forwardVncWheel(point, event);
    return;
  }
  if (manual.active && manual.surface === "explorer") {
    const maximum = Math.max(0, manual.entries.length - 8);
    manual.scrollOffset = THREE.MathUtils.clamp(
      manual.scrollOffset + (event.deltaY > 0 ? 1 : -1),
      0,
      maximum,
    );
  }
}, { passive: false, capture: true });

sceneCanvas.addEventListener("keydown", (event) => {
  livePressedCodes.add(visualCodeForPhysicalKey(event.code));
  if (state.vncConnected) {
    forwardVncKeyboard(event);
    event.preventDefault();
    return;
  }
  if (handleVirtualKeyDown(event)) event.preventDefault();
});

sceneCanvas.addEventListener("keyup", (event) => {
  livePressedCodes.delete(visualCodeForPhysicalKey(event.code));
  if (state.vncConnected) {
    forwardVncKeyboard(event);
    event.preventDefault();
  }
});

sceneCanvas.addEventListener("blur", () => {
  livePressedCodes.clear();
});

sceneCanvas.addEventListener("contextmenu", (event) => {
  const point = screenPoint(event);
  if (!point) return;
  event.preventDefault();
  if (state.vncConnected) return;
  if (!manual.active) {
    manual.active = true;
    manual.surface = "desktop";
  }
  if (manual.surface === "explorer" && manual.newFileName === undefined) {
    manual.selectedPath = visibleManualEntryAt(point.x, point.y)?.path;
    manual.contextMenu = point;
    sceneCanvas.focus({ preventScroll: true });
  }
});

resize();
animate();

async function connectDirectLocalServer() {
  if (!/^https?:$/.test(window.location.protocol)) return;
  const client = new Client(
    { name: "mcp-virtual-computer-browser", version: "0.2.2" },
    { capabilities: {} },
  );
  const transport = new StreamableHTTPClientTransport(new URL("/mcp", window.location.href));
  try {
    await client.connect(transport);
    const result = await client.callTool({ name: "computer_ui", arguments: {} });
    const payload = payloadOf(result);
    if (payload) {
      directClient = client;
      manual.hostConnected = !payload.error;
      applyPayload(payload);
    }
  } catch {
    await client.close().catch(() => undefined);
  }
}

let activityRevision: number | undefined;
let activityPollInFlight = false;

function beginExternalOperation(event: ActivityEvent) {
  manual.active = false;
  state.previousSurface = state.operation === "terminal_execute"
    ? "terminal"
    : state.operation === "idle"
      ? state.previousSurface
      : "file";
  state.operation = event.operation;
  state.arguments = event.arguments;
  state.payload = undefined;
  state.startedAt = performance.now();
  state.finishedAt = undefined;
}

async function pollActivity() {
  if (activityPollInFlight) return;
  activityPollInFlight = true;
  try {
    const after = activityRevision ?? 0;
    const response = await fetch(`/activity?after=${after}`, { cache: "no-store" });
    if (!response.ok) return;
    const feed = await response.json() as { revision: number; events: ActivityEvent[] };
    if (activityRevision === undefined) {
      activityRevision = feed.revision;
      return;
    }
    if (feed.revision < activityRevision) {
      activityRevision = feed.revision;
      return;
    }
    activityRevision = Math.max(activityRevision, feed.revision);
    if (manualToolCallInFlight || performance.now() < suppressActivityUntil) return;
    for (const event of feed.events) {
      if (event.phase === "request") beginExternalOperation(event);
      else {
        if (state.operation !== event.operation || state.finishedAt !== undefined) {
          beginExternalOperation(event);
        }
        state.arguments = event.arguments;
        if (event.payload) applyPayload(event.payload);
      }
    }
  } catch {
    // The MCP-hosted App uses tool lifecycle callbacks instead of this local feed.
  } finally {
    activityPollInFlight = false;
  }
}

void (async () => {
  await connectDirectLocalServer();
  if (directClient) {
    startRuntimePolling();
    await pollActivity();
    window.setInterval(() => void pollActivity(), 180);
    return;
  }
  try {
    await app.connect();
    const result = await app.callServerTool({ name: "computer_ui", arguments: {} });
    const payload = payloadOf(result);
    if (!payload || payload.error) throw new Error(payload?.error ?? "MCP App host unavailable");
    manual.hostConnected = true;
    applyPayload(payload);
    startRuntimePolling();
  } catch {
    // A packaged MCP App requires its host bridge; the local HTTP page uses /mcp.
  }
})();
