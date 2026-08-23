function Toggle({ checked, disabled, onChange }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      className={`or-switch ${checked ? 'or-switch--on' : ''}`}
      onClick={() => !disabled && onChange(!checked)}
    >
      <span className="or-switch-knob" />
    </button>
  )
}

export default Toggle
