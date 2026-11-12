export interface SpringState {
  value: number
  velocity: number
}

export function stepSpring(
  state: SpringState,
  target: number,
  dt: number,
  stiffness = 26,
  damping = 7,
): SpringState {
  const step = Math.min(Math.max(dt, 0), 0.05)
  const velocity =
    state.velocity +
    (target - state.value) * stiffness * step
  const damped = velocity * Math.exp(-damping * step)
  return {
    value: state.value + damped * step,
    velocity: damped,
  }
}
