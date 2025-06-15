'use client'

import { useState, useEffect } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import {
  HomeIcon,
  BellIcon,
  MagnifyingGlassIcon,
  ChartBarIcon,
  UserIcon,
  Cog6ToothIcon,
  ArrowRightOnRectangleIcon,
  Bars3Icon,
  XMarkIcon
} from '@heroicons/react/24/outline'
import { motion, AnimatePresence } from 'framer-motion'

const navigation = [
  { name: 'Dashboard', href: '/dashboard', icon: HomeIcon },
  { name: 'Job Alerts', href: '/dashboard/alerts', icon: BellIcon },
  { name: 'Job Search', href: '/dashboard/jobs', icon: MagnifyingGlassIcon },
  { name: 'Analytics', href: '/dashboard/analytics', icon: ChartBarIcon },
  { name: 'Profile', href: '/dashboard/profile', icon: UserIcon },
  { name: 'Settings', href: '/dashboard/settings', icon: Cog6ToothIcon },
]

interface DashboardLayoutProps {
  children: React.ReactNode
}

export default function DashboardLayout({ children }: DashboardLayoutProps) {
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [user, setUser] = useState<any>(null)
  const location = useLocation()
  const navigate = useNavigate()

  useEffect(() => {
    // Check if user is logged in
    const userData = localStorage.getItem('user')
    if (userData) {
      setUser(JSON.parse(userData))
    } else {
      // Redirect to login if not authenticated
      navigate('/auth/login')
    }
  }, [navigate])

  const handleSignOut = () => {
    localStorage.removeItem('user')
    navigate('/')
  }

  // Show loading while checking authentication
  if (!user) {
    return (
      <div className="min-h-screen bg-gradient-to-br from-slate-50 via-blue-50 to-indigo-100 flex items-center justify-center">
        <div className="text-center">
          <div className="animate-spin rounded-full h-16 w-16 border-4 border-primary-600 border-t-transparent mx-auto mb-4"></div>
          <p className="text-gray-600 font-medium">Loading your dashboard...</p>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 via-blue-50 to-indigo-100">
      {/* Background Elements */}
      <div className="fixed inset-0 overflow-hidden pointer-events-none">
        <div className="absolute -top-40 -right-40 w-80 h-80 bg-gradient-to-br from-purple-400 to-pink-400 rounded-full mix-blend-multiply filter blur-xl opacity-10 floating-animation"></div>
        <div className="absolute -bottom-40 -left-40 w-80 h-80 bg-gradient-to-br from-yellow-400 to-orange-400 rounded-full mix-blend-multiply filter blur-xl opacity-10 floating-animation" style={{ animationDelay: '2s' }}></div>
      </div>

      {/* Mobile sidebar */}
      <AnimatePresence>
        {sidebarOpen && (
          <>
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="fixed inset-0 z-40 lg:hidden"
            >
              <div className="fixed inset-0 bg-gray-600 bg-opacity-75" onClick={() => setSidebarOpen(false)} />
            </motion.div>

            <motion.div
              initial={{ x: -300 }}
              animate={{ x: 0 }}
              exit={{ x: -300 }}
              className="fixed inset-y-0 left-0 z-50 w-72 bg-white/90 backdrop-blur-xl shadow-2xl lg:hidden border-r border-white/20"
            >
              <div className="flex items-center justify-between h-20 px-6 border-b border-gray-200/50">
                <Link to="/dashboard" className="text-2xl font-bold text-gradient">
                  JobScout Pro
                </Link>
                <button
                  onClick={() => setSidebarOpen(false)}
                  className="text-gray-400 hover:text-gray-600 p-2 rounded-lg hover:bg-gray-100/50 transition-colors"
                >
                  <XMarkIcon className="h-6 w-6" />
                </button>
              </div>
              <nav className="mt-8 px-6">
                <div className="space-y-2">
                  {navigation.map((item) => {
                    const isActive = location.pathname === item.href
                    return (
                      <Link
                        key={item.name}
                        to={item.href}
                        className={`flex items-center px-4 py-3 rounded-xl text-sm font-semibold transition-all duration-200 ${
                          isActive
                            ? 'bg-gradient-to-r from-primary-500 to-purple-500 text-white shadow-lg'
                            : 'text-gray-700 hover:bg-white/50 hover:text-primary-600'
                        }`}
                        onClick={() => setSidebarOpen(false)}
                      >
                        <item.icon className="h-5 w-5 mr-3" />
                        {item.name}
                      </Link>
                    )
                  })}
                </div>
                <div className="mt-8 pt-8 border-t border-gray-200/50">
                  <div className="px-4 py-2 text-sm text-gray-600 font-medium">
                    Welcome, {user.name}
                  </div>
                  <button
                    onClick={handleSignOut}
                    className="flex items-center w-full px-4 py-3 text-sm font-semibold text-gray-700 hover:bg-red-50 hover:text-red-600 rounded-xl transition-all duration-200"
                  >
                    <ArrowRightOnRectangleIcon className="h-5 w-5 mr-3" />
                    Sign Out
                  </button>
                </div>
              </nav>
            </motion.div>
          </>
        )}
      </AnimatePresence>

      {/* Desktop sidebar */}
      <div className="hidden lg:fixed lg:inset-y-0 lg:flex lg:w-72 lg:flex-col">
        <div className="flex flex-col flex-grow bg-white/90 backdrop-blur-xl border-r border-white/20 shadow-xl">
          <div className="flex items-center h-20 px-6 border-b border-gray-200/50">
            <Link to="/dashboard" className="text-2xl font-bold text-gradient">
              JobScout Pro
            </Link>
          </div>
          <nav className="mt-8 flex-1 px-6">
            <div className="space-y-2">
              {navigation.map((item) => {
                const isActive = location.pathname === item.href
                return (
                  <Link
                    key={item.name}
                    to={item.href}
                    className={`flex items-center px-4 py-3 rounded-xl text-sm font-semibold transition-all duration-200 ${
                      isActive
                        ? 'bg-gradient-to-r from-primary-500 to-purple-500 text-white shadow-lg'
                        : 'text-gray-700 hover:bg-white/50 hover:text-primary-600'
                    }`}
                  >
                    <item.icon className="h-5 w-5 mr-3" />
                    {item.name}
                  </Link>
                )
              })}
            </div>
            <div className="mt-8 pt-8 border-t border-gray-200/50">
              <div className="px-4 py-2 text-sm text-gray-600 font-medium">
                Welcome, {user.name}
              </div>
              <button
                onClick={handleSignOut}
                className="flex items-center w-full px-4 py-3 text-sm font-semibold text-gray-700 hover:bg-red-50 hover:text-red-600 rounded-xl transition-all duration-200"
              >
                <ArrowRightOnRectangleIcon className="h-5 w-5 mr-3" />
                Sign Out
              </button>
            </div>
          </nav>
        </div>
      </div>

      {/* Main content */}
      <div className="lg:pl-72">
        {/* Top bar */}
        <div className="sticky top-0 z-10 bg-white/80 backdrop-blur-md border-b border-white/20 lg:hidden">
          <div className="flex items-center justify-between h-16 px-4">
            <button
              onClick={() => setSidebarOpen(true)}
              className="text-gray-400 hover:text-gray-600 p-2 rounded-lg hover:bg-gray-100/50 transition-colors"
            >
              <Bars3Icon className="h-6 w-6" />
            </button>
            <Link to="/dashboard" className="text-xl font-bold text-gradient">
              JobScout Pro
            </Link>
            <div className="w-10" /> {/* Spacer */}
          </div>
        </div>

        {/* Page content */}
        <main className="p-6 lg:p-10">
          {children}
        </main>
      </div>
    </div>
  )
}