'use client'

import { motion } from 'framer-motion'

interface StatsCardProps {
  title: string
  value: number | string
  icon: React.ComponentType<{ className?: string }>
  color: 'blue' | 'green' | 'yellow' | 'purple' | 'red'
  badge?: string
  trend?: {
    value: number
    isPositive: boolean
  }
}

const colorClasses = {
  blue: {
    bg: 'bg-blue-50',
    icon: 'text-blue-600',
    text: 'text-blue-600'
  },
  green: {
    bg: 'bg-green-50',
    icon: 'text-green-600',
    text: 'text-green-600'
  },
  yellow: {
    bg: 'bg-yellow-50',
    icon: 'text-yellow-600',
    text: 'text-yellow-600'
  },
  purple: {
    bg: 'bg-purple-50',
    icon: 'text-purple-600',
    text: 'text-purple-600'
  },
  red: {
    bg: 'bg-red-50',
    icon: 'text-red-600',
    text: 'text-red-600'
  }
}

export default function StatsCard({ title, value, icon: Icon, color, badge, trend }: StatsCardProps) {
  const colors = colorClasses[color]

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      className="card hover:shadow-md transition-shadow duration-200"
    >
      <div className="flex items-center">
        <div className={`p-3 rounded-lg ${colors.bg}`}>
          <Icon className={`h-6 w-6 ${colors.icon}`} />
        </div>
        <div className="ml-4 flex-1">
          <div className="flex items-center justify-between">
            <p className="text-sm font-medium text-gray-600">{title}</p>
            {badge && (
              <span className="px-2 py-1 text-xs bg-primary-100 text-primary-800 rounded-full">
                {badge}
              </span>
            )}
          </div>
          <div className="flex items-center mt-1">
            <p className="text-2xl font-semibold text-gray-900">{value}</p>
            {trend && (
              <span className={`ml-2 text-sm ${
                trend.isPositive ? 'text-success-600' : 'text-error-600'
              }`}>
                {trend.isPositive ? '+' : '-'}{Math.abs(trend.value)}%
              </span>
            )}
          </div>
        </div>
      </div>
    </motion.div>
  )
}