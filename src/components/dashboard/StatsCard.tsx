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
    bg: 'from-blue-500 to-cyan-500',
    icon: 'text-white',
    text: 'text-blue-600'
  },
  green: {
    bg: 'from-green-500 to-emerald-500',
    icon: 'text-white',
    text: 'text-green-600'
  },
  yellow: {
    bg: 'from-yellow-500 to-orange-500',
    icon: 'text-white',
    text: 'text-yellow-600'
  },
  purple: {
    bg: 'from-purple-500 to-pink-500',
    icon: 'text-white',
    text: 'text-purple-600'
  },
  red: {
    bg: 'from-red-500 to-pink-500',
    icon: 'text-white',
    text: 'text-red-600'
  }
}

export default function StatsCard({ title, value, icon: Icon, color, badge, trend }: StatsCardProps) {
  const colors = colorClasses[color]

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      className="card hover:shadow-2xl transition-all duration-300 group hover:-translate-y-1"
    >
      <div className="flex items-center">
        <div className={`p-4 rounded-2xl bg-gradient-to-r ${colors.bg} shadow-lg group-hover:shadow-xl transition-shadow duration-300`}>
          <Icon className={`h-8 w-8 ${colors.icon}`} />
        </div>
        <div className="ml-6 flex-1">
          <div className="flex items-center justify-between">
            <p className="text-sm font-semibold text-gray-600 uppercase tracking-wide">{title}</p>
            {badge && (
              <span className="px-3 py-1 text-xs bg-gradient-to-r from-primary-100 to-purple-100 text-primary-800 rounded-full font-semibold">
                {badge}
              </span>
            )}
          </div>
          <div className="flex items-center mt-2">
            <p className="text-3xl font-bold text-gray-900">{value}</p>
            {trend && (
              <span className={`ml-3 text-sm font-semibold px-2 py-1 rounded-full ${
                trend.isPositive 
                  ? 'text-green-700 bg-green-100' 
                  : 'text-red-700 bg-red-100'
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