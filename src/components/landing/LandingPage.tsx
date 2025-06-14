'use client'

import { useState } from 'react'
import { motion } from 'framer-motion'
import { 
  ChartBarIcon, 
  BellIcon, 
  MagnifyingGlassIcon,
  CheckIcon,
  ArrowRightIcon,
  SparklesIcon,
  ClockIcon,
  TrophyIcon
} from '@heroicons/react/24/outline'
import Link from 'next/link'

const features = [
  {
    icon: ChartBarIcon,
    title: 'Smart Analytics',
    description: 'Get insights into salary trends, market demand, and competition analysis to make informed career decisions.'
  },
  {
    icon: BellIcon,
    title: 'Real-time Alerts',
    description: 'Receive instant notifications when jobs matching your criteria are posted, never miss an opportunity.'
  },
  {
    icon: MagnifyingGlassIcon,
    title: 'Advanced Search',
    description: 'Filter by salary range, company size, remote options, and skills with our intelligent matching system.'
  },
  {
    icon: SparklesIcon,
    title: 'AI-Powered Recommendations',
    description: 'Our AI analyzes your profile and suggests the most relevant opportunities for your career growth.'
  },
  {
    icon: ClockIcon,
    title: 'Application Tracking',
    description: 'Manage your job applications with our built-in CRM system and never lose track of opportunities.'
  },
  {
    icon: TrophyIcon,
    title: 'Resume Optimization',
    description: 'Get ATS compatibility scores and keyword suggestions to improve your resume performance.'
  }
]

const pricingPlans = [
  {
    name: 'Free',
    price: '$0',
    period: 'forever',
    description: 'Perfect for getting started',
    features: [
      '1 active job alert',
      'Weekly alert checks',
      'Basic job search (7 days)',
      '10 job views per month',
      'Community support'
    ],
    cta: 'Get Started Free',
    popular: false
  },
  {
    name: 'Pro',
    price: '$5',
    period: 'per month',
    description: 'Everything you need for serious job hunting',
    features: [
      'Unlimited job alerts',
      'Real-time checks (every 30 min)',
      'Full job history (90 days)',
      'Advanced filtering & sorting',
      'Email notifications',
      'Application tracking',
      'Salary insights',
      'Priority support'
    ],
    cta: 'Start Pro Trial',
    popular: true
  }
]

export default function LandingPage() {
  const [email, setEmail] = useState('')

  return (
    <div className="min-h-screen bg-white">
      {/* Navigation */}
      <nav className="fixed top-0 w-full bg-white/80 backdrop-blur-md z-50 border-b border-gray-200">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex justify-between items-center h-16">
            <div className="flex items-center">
              <div className="flex-shrink-0">
                <h1 className="text-2xl font-bold text-gradient">JobScout Pro</h1>
              </div>
            </div>
            <div className="flex items-center space-x-4">
              <Link href="/auth/login" className="text-gray-700 hover:text-primary-600 font-medium">
                Sign In
              </Link>
              <Link href="/auth/signup" className="btn-primary">
                Get Started
              </Link>
            </div>
          </div>
        </div>
      </nav>

      {/* Hero Section */}
      <section className="pt-24 pb-16 px-4 sm:px-6 lg:px-8">
        <div className="max-w-7xl mx-auto">
          <div className="text-center">
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.6 }}
            >
              <h1 className="text-4xl sm:text-6xl font-bold text-gray-900 mb-6">
                Your AI-powered{' '}
                <span className="text-gradient">career intelligence</span>{' '}
                platform
              </h1>
              <p className="text-xl text-gray-600 mb-8 max-w-3xl mx-auto">
                Transform your job search with smart alerts, market insights, and AI-powered recommendations. 
                Never miss your dream job again.
              </p>
            </motion.div>

            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.6, delay: 0.2 }}
              className="flex flex-col sm:flex-row gap-4 justify-center items-center mb-12"
            >
              <div className="flex">
                <input
                  type="email"
                  placeholder="Enter your email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  className="input-field rounded-r-none w-80"
                />
                <Link 
                  href="/auth/signup"
                  className="bg-primary-600 hover:bg-primary-700 text-white px-6 py-3 rounded-r-lg font-medium transition-colors duration-200 flex items-center"
                >
                  Start Free Trial
                  <ArrowRightIcon className="ml-2 h-4 w-4" />
                </Link>
              </div>
            </motion.div>

            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.6, delay: 0.4 }}
              className="text-sm text-gray-500"
            >
              ✨ No credit card required • 7-day free trial • Cancel anytime
            </motion.div>
          </div>
        </div>
      </section>

      {/* Features Section */}
      <section className="py-16 bg-gray-50">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="text-center mb-16">
            <h2 className="text-3xl font-bold text-gray-900 mb-4">
              Everything you need for modern job hunting
            </h2>
            <p className="text-xl text-gray-600 max-w-2xl mx-auto">
              From AI-powered insights to real-time alerts, we've built the complete toolkit for your career success.
            </p>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-8">
            {features.map((feature, index) => (
              <motion.div
                key={feature.title}
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.6, delay: index * 0.1 }}
                className="card hover:shadow-lg transition-shadow duration-300"
              >
                <div className="flex items-center mb-4">
                  <div className="flex-shrink-0">
                    <feature.icon className="h-8 w-8 text-primary-600" />
                  </div>
                  <h3 className="ml-3 text-lg font-semibold text-gray-900">
                    {feature.title}
                  </h3>
                </div>
                <p className="text-gray-600">
                  {feature.description}
                </p>
              </motion.div>
            ))}
          </div>
        </div>
      </section>

      {/* Pricing Section */}
      <section className="py-16">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="text-center mb-16">
            <h2 className="text-3xl font-bold text-gray-900 mb-4">
              Simple, transparent pricing
            </h2>
            <p className="text-xl text-gray-600">
              Start free, upgrade when you're ready to supercharge your job search.
            </p>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-8 max-w-4xl mx-auto">
            {pricingPlans.map((plan, index) => (
              <motion.div
                key={plan.name}
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.6, delay: index * 0.2 }}
                className={`card relative ${plan.popular ? 'ring-2 ring-primary-500 shadow-glow' : ''}`}
              >
                {plan.popular && (
                  <div className="absolute -top-4 left-1/2 transform -translate-x-1/2">
                    <span className="bg-primary-600 text-white px-4 py-1 rounded-full text-sm font-medium">
                      Most Popular
                    </span>
                  </div>
                )}
                
                <div className="text-center mb-6">
                  <h3 className="text-2xl font-bold text-gray-900 mb-2">{plan.name}</h3>
                  <div className="mb-2">
                    <span className="text-4xl font-bold text-gray-900">{plan.price}</span>
                    <span className="text-gray-600 ml-1">/{plan.period}</span>
                  </div>
                  <p className="text-gray-600">{plan.description}</p>
                </div>

                <ul className="space-y-3 mb-8">
                  {plan.features.map((feature) => (
                    <li key={feature} className="flex items-center">
                      <CheckIcon className="h-5 w-5 text-success-500 mr-3 flex-shrink-0" />
                      <span className="text-gray-700">{feature}</span>
                    </li>
                  ))}
                </ul>

                <Link
                  href="/auth/signup"
                  className={`w-full text-center py-3 px-4 rounded-lg font-medium transition-colors duration-200 ${
                    plan.popular
                      ? 'bg-primary-600 hover:bg-primary-700 text-white'
                      : 'bg-gray-100 hover:bg-gray-200 text-gray-900'
                  }`}
                >
                  {plan.cta}
                </Link>
              </motion.div>
            ))}
          </div>
        </div>
      </section>

      {/* CTA Section */}
      <section className="py-16 bg-gradient-primary">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 text-center">
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.6 }}
          >
            <h2 className="text-3xl font-bold text-white mb-4">
              Ready to transform your job search?
            </h2>
            <p className="text-xl text-blue-100 mb-8 max-w-2xl mx-auto">
              Join thousands of professionals who've found their dream jobs with JobScout Pro.
            </p>
            <Link
              href="/auth/signup"
              className="bg-white text-primary-600 hover:bg-gray-50 font-semibold py-3 px-8 rounded-lg transition-colors duration-200 inline-flex items-center"
            >
              Start Your Free Trial
              <ArrowRightIcon className="ml-2 h-5 w-5" />
            </Link>
          </motion.div>
        </div>
      </section>

      {/* Footer */}
      <footer className="bg-gray-900 text-white py-12">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="text-center">
            <h3 className="text-2xl font-bold mb-4">JobScout Pro</h3>
            <p className="text-gray-400 mb-8">
              Your AI-powered career intelligence platform
            </p>
            <div className="flex justify-center space-x-8 text-sm text-gray-400">
              <Link href="/privacy" className="hover:text-white">Privacy Policy</Link>
              <Link href="/terms" className="hover:text-white">Terms of Service</Link>
              <Link href="/contact" className="hover:text-white">Contact</Link>
            </div>
            <div className="mt-8 pt-8 border-t border-gray-800 text-center text-gray-400">
              <p>&copy; 2024 JobScout Pro. All rights reserved.</p>
            </div>
          </div>
        </div>
      </footer>
    </div>
  )
}