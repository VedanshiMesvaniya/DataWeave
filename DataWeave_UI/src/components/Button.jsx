import clsx from 'clsx'

export default function Button({
  as: Component = 'button',
  className,
  variant = 'primary',
  type = 'button',
  ...props
}) {
  return (
    <Component
      type={Component === 'button' ? type : undefined}
      className={clsx(
        variant === 'primary' && 'primary-button',
        variant === 'danger' && 'primary-button primary-button--danger',
        (variant === 'secondary' || variant === 'ghost') && 'secondary-button',
        className,
      )}
      {...props}
    />
  )
}
