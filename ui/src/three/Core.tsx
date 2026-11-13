/**
 * The Core: the bot's presence on screen. A brushed-metal sculpture (Higgsfield image-to-3D mesh,
 * ui/public/assets/core.glb) cradles a glass orb whose inner light is the bot's state:
 * green running, blue idle/replay, amber paused, red alert. It pulses once per new decision
 * (brighter when approved), turns faster while things happen, and dims when the market is closed.
 * Three thin rings around the orb are the mission's sleeves (stocks, crypto, gold & CFDs): a ring
 * glows in its sleeve's colour and precesses while that sleeve has something live, rests dim
 * and still when it has not, and turns amber when the sleeve needs attention.
 */
import { Component, Suspense, useEffect, useMemo, useRef, type ReactNode } from 'react';
import { Canvas, useFrame, useLoader, useThree } from '@react-three/fiber';
import * as THREE from 'three';
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { MeshoptDecoder } from 'three/examples/jsm/libs/meshopt_decoder.module.js';
import type { BotTone } from '../app/botStatus';

const TONE: Record<BotTone, string> = {
  running: '#30d158',
  idle: '#5e9cff',
  replay: '#0a84ff',
  paused: '#fab219',
  alert: '#ff453a',
  offline: '#8e8e93',
};

/** One sleeve's ring: its colour, whether something is live in it, whether it needs attention. */
export interface SleeveLight {
  color: string;
  live: boolean;
  warn: boolean;
}

export interface CoreProps {
  tone: BotTone;
  /** Increments on every new decision. */
  pulse: number;
  pulseApproved: boolean | null;
  dim: boolean;
  dark: boolean;
  model?: string;
  orbY?: number;
  /** The mission's sleeves, drawn as rings around the orb (none: no rings). */
  sleeves?: SleeveLight[];
  /** Fires once the model has rendered its first frame. */
  onReady?: () => void;
}

function Environment() {
  const { gl, scene } = useThree();
  useEffect(() => {
    const pm = new THREE.PMREMGenerator(gl);
    const env = pm.fromScene(new RoomEnvironment(), 0.04).texture;
    scene.environment = env;
    return () => {
      scene.environment = null;
      env.dispose();
      pm.dispose();
    };
  }, [gl, scene]);
  return null;
}

/** Calls back after the first frame that includes the model (for the crossfade from the still). */
function ReadySignal({ onReady }: { onReady?: () => void }) {
  const fired = useRef(false);
  useFrame(() => {
    if (!fired.current) {
      fired.current = true;
      onReady?.();
    }
  });
  return null;
}

function Sculpture({ url, dark }: { url: string; dark: boolean }) {
  const gltf = useLoader(GLTFLoader, url, (loader) => {
    loader.setMeshoptDecoder(MeshoptDecoder);
  });
  const material = useMemo(
    () =>
      new THREE.MeshPhysicalMaterial({
        color: dark ? '#c4c7cd' : '#d4d6db',
        metalness: 1,
        roughness: 0.34,
        clearcoat: 0.35,
        clearcoatRoughness: 0.28,
        envMapIntensity: dark ? 1.25 : 1.15,
      }),
    [dark],
  );
  const scene = useMemo(() => {
    const root = gltf.scene.clone(true);
    root.traverse((o) => {
      const mesh = o as THREE.Mesh;
      if (mesh.isMesh) {
        if (!mesh.geometry.attributes.normal) mesh.geometry.computeVertexNormals();
        mesh.material = material;
        mesh.castShadow = true;
      }
    });
    return root;
  }, [gltf.scene, material]);
  return <primitive object={scene} />;
}

function Orb({ tone, pulse, pulseApproved, dim, orbY }: { tone: BotTone; pulse: number; pulseApproved: boolean | null; dim: boolean; orbY: number }) {
  const core = useRef<THREE.Mesh>(null);
  const glass = useRef<THREE.Mesh>(null);
  const light = useRef<THREE.PointLight>(null);
  const pulseAt = useRef(-10);
  const color = useMemo(() => new THREE.Color(TONE[tone]), [tone]);
  const target = useRef(new THREE.Color(TONE[tone]));
  const strength = useRef(1);

  useEffect(() => {
    target.current.set(TONE[tone]);
  }, [tone]);
  useEffect(() => {
    if (pulse > 0) {
      pulseAt.current = performance.now() / 1000;
      strength.current = pulseApproved ? 1.6 : 1;
    }
  }, [pulse, pulseApproved]);

  useFrame(({ clock }, delta) => {
    const t = clock.getElapsedTime();
    const since = performance.now() / 1000 - pulseAt.current;
    const kick = Math.exp(-since * 3.2) * strength.current;
    color.lerp(target.current, Math.min(1, delta * 3));
    const breathe = dim ? 0.25 : 0.6 + Math.sin(t * 1.6) * 0.08;
    const s = 1 + kick * 0.14;
    core.current?.scale.setScalar(s);
    glass.current?.scale.setScalar(1 + kick * 0.05);
    if (core.current) (core.current.material as THREE.MeshBasicMaterial).color.copy(color).multiplyScalar(0.55 + breathe + kick * 0.9);
    if (light.current) {
      light.current.color.copy(color);
      light.current.intensity = (dim ? 0.5 : 1.6) + kick * 6;
    }
  });

  return (
    <group position={[0, orbY, 0]}>
      <mesh ref={core}>
        <sphereGeometry args={[0.055, 48, 48]} />
        <meshBasicMaterial toneMapped={false} />
      </mesh>
      <mesh ref={glass}>
        <sphereGeometry args={[0.11, 64, 64]} />
        <meshPhysicalMaterial
          transmission={1}
          roughness={0.16}
          thickness={0.35}
          ior={1.4}
          metalness={0}
          clearcoat={1}
          clearcoatRoughness={0.1}
          envMapIntensity={1.2}
          transparent
        />
      </mesh>
      <pointLight ref={light} distance={1.4} decay={2} />
    </group>
  );
}

const WARN = '#fab219';
// Radius and tilt of each ring: nested, crossing like orbits, all inside the sculpture's own rings.
const RINGS: { radius: number; tilt: [number, number, number]; spin: number }[] = [
  { radius: 0.16, tilt: [Math.PI / 2, 0, 0], spin: 0.55 },
  { radius: 0.186, tilt: [Math.PI / 2.9, 0, Math.PI / 3.1], spin: -0.42 },
  { radius: 0.212, tilt: [Math.PI / 2.9, 0, -Math.PI / 3.1], spin: 0.34 },
];

function SleeveRings({ sleeves, orbY, dim }: { sleeves: SleeveLight[]; orbY: number; dim: boolean }) {
  const groups = useRef<(THREE.Group | null)[]>([]);
  const meshes = useRef<(THREE.Mesh | null)[]>([]);
  const colors = useMemo(() => sleeves.map(() => new THREE.Color('#888888')), [sleeves.length]); // eslint-disable-line react-hooks/exhaustive-deps
  const target = useMemo(() => new THREE.Color(), []);
  const reduce = useMemo(() => matchMedia('(prefers-reduced-motion: reduce)').matches, []);

  useFrame(({ clock }, delta) => {
    const t = clock.getElapsedTime();
    sleeves.forEach((sl, i) => {
      const mesh = meshes.current[i];
      const group = groups.current[i];
      if (!mesh || !group) return;
      target.set(sl.warn ? WARN : sl.color);
      colors[i].lerp(target, Math.min(1, delta * 3));
      const level = sl.live ? 1.15 + (reduce ? 0 : Math.sin(t * 2.2 + i * 2.1) * 0.28) : sl.warn ? 0.8 : dim ? 0.42 : 0.55;
      const mat = mesh.material as THREE.MeshBasicMaterial;
      mat.color.copy(colors[i]).multiplyScalar(level);
      mat.opacity = sl.live || sl.warn ? 1 : 0.85;
      // A live sleeve's ring precesses; an idle one rests where it is.
      if (sl.live && !reduce) group.rotation.y += delta * RINGS[i % RINGS.length].spin;
    });
  });

  return (
    <group position={[0, orbY, 0]}>
      {sleeves.slice(0, RINGS.length).map((_, i) => (
        <group key={i} ref={(g) => void (groups.current[i] = g)}>
          <mesh ref={(m) => void (meshes.current[i] = m)} rotation={RINGS[i].tilt}>
            <torusGeometry args={[RINGS[i].radius, 0.006, 12, 96]} />
            <meshBasicMaterial toneMapped={false} transparent />
          </mesh>
        </group>
      ))}
    </group>
  );
}

function Rig({ children, activity, dim }: { children: ReactNode; activity: number; dim: boolean }) {
  const group = useRef<THREE.Group>(null);
  const speed = useRef(0.12);
  const lastActivity = useRef(activity);
  const boostUntil = useRef(0);
  const reduce = useMemo(() => matchMedia('(prefers-reduced-motion: reduce)').matches, []);
  // Drag to spin: the sculpture follows the pointer and keeps its momentum when released.
  const { gl } = useThree();
  const drag = useRef<{ x: number; active: boolean; velocity: number }>({ x: 0, active: false, velocity: 0 });
  useEffect(() => {
    const el = gl.domElement;
    el.style.cursor = 'grab';
    el.style.touchAction = 'pan-y';
    const down = (e: PointerEvent) => {
      drag.current = { x: e.clientX, active: true, velocity: 0 };
      el.setPointerCapture(e.pointerId);
      el.style.cursor = 'grabbing';
    };
    const move = (e: PointerEvent) => {
      if (!drag.current.active || !group.current) return;
      const dx = (e.clientX - drag.current.x) * 0.012;
      drag.current.x = e.clientX;
      group.current.rotation.y += dx;
      drag.current.velocity = dx * 60;
    };
    const up = (e: PointerEvent) => {
      drag.current.active = false;
      if (el.hasPointerCapture(e.pointerId)) el.releasePointerCapture(e.pointerId);
      el.style.cursor = 'grab';
    };
    el.addEventListener('pointerdown', down);
    el.addEventListener('pointermove', move);
    el.addEventListener('pointerup', up);
    el.addEventListener('pointercancel', up);
    return () => {
      el.removeEventListener('pointerdown', down);
      el.removeEventListener('pointermove', move);
      el.removeEventListener('pointerup', up);
      el.removeEventListener('pointercancel', up);
    };
  }, [gl]);
  useFrame(({ pointer, clock }, delta) => {
    const g = group.current;
    if (!g) return;
    const now = clock.getElapsedTime();
    if (activity !== lastActivity.current) {
      lastActivity.current = activity;
      boostUntil.current = now + 2.5;
    }
    const base = dim ? 0.05 : 0.14;
    const goal = reduce ? 0 : now < boostUntil.current ? base * 4 : base;
    speed.current += (goal - speed.current) * Math.min(1, delta * 2);
    if (drag.current.active) return;
    // Thrown momentum decays back to the idle turn.
    drag.current.velocity *= Math.exp(-delta * 2.2);
    g.rotation.y += delta * (speed.current + drag.current.velocity);
    if (!reduce) {
      g.rotation.x += (pointer.y * 0.08 - g.rotation.x) * Math.min(1, delta * 2);
      g.position.x += (pointer.x * 0.02 - g.position.x) * Math.min(1, delta * 2);
    }
  });
  return <group ref={group}>{children}</group>;
}

/** A soft elliptical contact shadow: a radial gradient on a plane under the sculpture. */
function SoftShadow({ opacity }: { opacity: number }) {
  const texture = useMemo(() => {
    const c = document.createElement('canvas');
    c.width = c.height = 256;
    const g = c.getContext('2d')!;
    const grad = g.createRadialGradient(128, 128, 0, 128, 128, 128);
    grad.addColorStop(0, 'rgba(0,0,0,1)');
    grad.addColorStop(0.45, 'rgba(0,0,0,0.45)');
    grad.addColorStop(1, 'rgba(0,0,0,0)');
    g.fillStyle = grad;
    g.fillRect(0, 0, 256, 256);
    const t = new THREE.CanvasTexture(c);
    t.colorSpace = THREE.SRGBColorSpace;
    return t;
  }, []);
  return (
    <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -0.505, 0]} scale={[1.15, 0.7, 1]}>
      <planeGeometry args={[1, 1]} />
      <meshBasicMaterial map={texture} transparent opacity={opacity} depthWrite={false} toneMapped={false} />
    </mesh>
  );
}

class GlBoundary extends Component<{ fallback: ReactNode; children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}

/**
 * Probe once per page and release the probe context immediately. Browsers cap live WebGL contexts
 * (about 16); probing on every render would evict the real scene's context.
 */
let webgl: boolean | null = null;
function webglAvailable(): boolean {
  if (webgl !== null) return webgl;
  try {
    const c = document.createElement('canvas');
    const gl = (c.getContext('webgl2') ?? c.getContext('webgl')) as WebGLRenderingContext | null;
    webgl = Boolean(gl);
    gl?.getExtension('WEBGL_lose_context')?.loseContext();
  } catch {
    webgl = false;
  }
  return webgl;
}

export default function Core({ tone, pulse, pulseApproved, dim, dark, model = '/assets/core.glb', orbY = 0.06, sleeves, onReady }: CoreProps) {
  const fallback = <img src="/assets/core.png" alt="" style={{ width: '100%', height: '100%', objectFit: 'contain', opacity: dim ? 0.7 : 1 }} />;
  if (!webglAvailable()) return fallback;
  return (
    <GlBoundary fallback={fallback}>
      <Canvas
        dpr={[1, 2]}
        camera={{ position: [0, 0.02, 2.35], fov: 30 }}
        gl={{ antialias: true, alpha: true, toneMapping: THREE.ACESFilmicToneMapping, toneMappingExposure: dark ? 1.0 : 1.08 }}
        style={{ width: '100%', height: '100%' }}
        aria-hidden
      >
        <Environment />
        <ambientLight intensity={dark ? 0.22 : 0.3} />
        <directionalLight position={[2, 3, 2]} intensity={dark ? 0.95 : 1.1} />
        <Suspense fallback={null}>
          <ReadySignal onReady={onReady} />
          <Rig activity={pulse} dim={dim}>
            <Sculpture url={model} dark={dark} />
            <Orb tone={tone} pulse={pulse} pulseApproved={pulseApproved} dim={dim} orbY={orbY} />
            {sleeves && sleeves.length > 0 && <SleeveRings sleeves={sleeves} orbY={orbY} dim={dim} />}
          </Rig>
          <SoftShadow opacity={dark ? 0.6 : 0.28} />
        </Suspense>
      </Canvas>
    </GlBoundary>
  );
}
