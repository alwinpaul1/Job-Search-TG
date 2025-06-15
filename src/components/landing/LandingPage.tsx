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
  TrophyIcon,
  StarIcon,
  PlayIcon
} from '@heroicons/react/24/outline'
import { Link } from 'react-router-dom'

const features = [
  {
    icon: ChartBarIcon,
    title: 'Smart Analytics',
    description: 'Get insights into salary trends, market demand, and competition analysis to make informed career decisions.',
    color: 'from-blue-500 to-cyan-500'
  },
  {
    icon: BellIcon,
    title: 'Real-time Alerts',
    description: 'Receive instant notifications when jobs matching your criteria are posted, never miss an opportunity.',
    color: 'from-purple-500 to-pink-500'
  },
  {
    icon: MagnifyingGlassIcon,
    title: 'Advanced Search',
    description: 'Filter by salary range, company size, remote options, and skills with our intelligent matching system.',
    color: 'from-green-500 to-emerald-500'
  },
  {
    icon: SparklesIcon,
    title: 'AI-Powered Recommendations',
    description: 'Our AI analyzes your profile and suggests the most relevant opportunities for your career growth.',
    color: 'from-yellow-500 to-orange-500'
  },
  {
    icon: ClockIcon,
    title: 'Application Tracking',
    description: 'Manage your job applications with our built-in CRM system and never lose track of opportunities.',
    color: 'from-indigo-500 to-purple-500'
  },
  {
    icon: TrophyIcon,
    title: 'Resume Optimization',
    description: 'Get ATS compatibility scores and keyword suggestions to improve your resume performance.',
    color: 'from-red-500 to-pink-500'
  }
]

const testimonials = [
  {
    name: 'Sarah Chen',
    role: 'Software Engineer at Google',
    image: 'https://images.pexels.com/photos/774909/pexels-photo-774909.jpeg?auto=compress&cs=tinysrgb&w=150&h=150&dpr=2',
    content: 'JobScout Pro helped me land my dream job at Google. The AI recommendations were spot-on!'
  },
  {
    name: 'Marcus Johnson',
    role: 'Product Manager at Microsoft',
    image: 'https://images.pexels.com/photos/1222271/pexels-photo-1222271.jpeg?auto=compress&cs=tinysrgb&w=150&h=150&dpr=2',
    content: 'The real-time alerts feature is incredible. I got notified about my current role within minutes of it being posted.'
  },
  {
    name: 'Emily Rodriguez',
    role: 'UX Designer at Airbnb',
    image: 'https://images.pexels.com/photos/1239291/pexels-photo-1239291.jpeg?auto=compress&cs=tinysrgb&w=150&h=150&dpr=2',
    content: 'The analytics dashboard gave me insights I never had before. It completely changed my job search strategy.'
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

const stats = [
  { number: '50K+', label: 'Jobs Found' },
  { number: '10K+', label: 'Happy Users' },
  { number: '500+', label: 'Companies' },
  { number: '95%', label: 'Success Rate' }
]

export default function LandingPage() {
  const [email, setEmail] = useState('')

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 via-blue-50 to-indigo-100">
      {/* Background Elements */}
      <div className="fixed inset-0 overflow-hidden pointer-events-none">
        <div className="absolute -top-40 -right-40 w-80 h-80 bg-gradient-to-br from-purple-400 to-pink-400 rounded-full mix-blend-multiply filter blur-xl opacity-20 floating-animation"></div>
        <div className="absolute -bottom-40 -left-40 w-80 h-80 bg-gradient-to-br from-yellow-400 to-orange-400 rounded-full mix-blend-multiply filter blur-xl opacity-20 floating-animation" style={{ animationDelay: '2s' }}></div>
        <div className="absolute top-40 left-1/2 w-80 h-80 bg-gradient-to-br from-blue-400 to-cyan-400 rounded-full mix-blend-multiply filter blur-xl opacity-20 floating-animation" style={{ animationDelay: '4s' }}></div>
      </div>

      {/* Navigation */}
      <nav className="fixed top-0 w-full bg-white/80 backdrop-blur-md z-50 border-b border-white/20">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex justify-between items-center h-20">
            <div className="flex items-center">
              <div className="flex-shrink-0">
                <h1 className="text-3xl font-bold text-gradient">JobScout Pro</h1>
              </div>
            </div>
            <div className="flex items-center space-x-6">
              <Link to="/auth/login" className="text-gray-700 hover:text-primary-600 font-semibold transition-colors">
                Sign In
              </Link>
              <Link to="/auth/signup" className="btn-primary">
                Get Started
              </Link>
            </div>
          </div>
        </div>
      </nav>

      {/* Hero Section */}
      <section className="pt-32 pb-20 px-4 sm:px-6 lg:px-8 relative">
        <div className="max-w-7xl mx-auto">
          <div className="text-center">
            <motion.div
              initial={{ opacity: 0, y: 30 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.8 }}
            >
              <div className="inline-flex items-center px-4 py-2 bg-gradient-to-r from-primary-100 to-purple-100 rounded-full text-primary-700 font-semibold text-sm mb-8">
                <SparklesIcon className="h-4 w-4 mr-2" />
                AI-Powered Job Discovery Platform
              </div>
              <h1 className="text-5xl sm:text-7xl font-bold text-gray-900 mb-8 leading-tight">
                Your AI-powered{' '}
                <span className="text-gradient">career intelligence</span>{' '}
                platform
              </h1>
              <p className="text-xl text-gray-600 mb-12 max-w-3xl mx-auto leading-relaxed">
                Transform your job search with smart alerts, market insights, and AI-powered recommendations. 
                Never miss your dream job again with our cutting-edge technology.
              </p>
            </motion.div>

            <motion.div
              initial={{ opacity: 0, y: 30 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.8, delay: 0.2 }}
              className="flex flex-col sm:flex-row gap-6 justify-center items-center mb-16"
            >
              <div className="flex bg-white/80 backdrop-blur-sm rounded-2xl shadow-lg p-2 border border-white/20">
                <input
                  type="email"
                  placeholder="Enter your email address"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  className="input-field border-0 bg-transparent w-80 focus:ring-0"
                />
                <Link 
                  to="/auth/signup"
                  className="bg-gradient-to-r from-primary-600 to-primary-700 hover:from-primary-700 hover:to-primary-800 text-white px-8 py-3 rounded-xl font-semibold transition-all duration-300 flex items-center shadow-lg hover:shadow-xl transform hover:-translate-y-0.5"
                >
                  Start Free Trial
                  <ArrowRightIcon className="ml-2 h-5 w-5" />
                </Link>
              </div>
            </motion.div>

            <motion.div
              initial={{ opacity: 0, y: 30 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.8, delay: 0.4 }}
              className="flex items-center justify-center space-x-8 text-sm text-gray-500"
            >
              <div className="flex items-center">
                <CheckIcon className="h-4 w-4 text-green-500 mr-2" />
                No credit card required
              </div>
              <div className="flex items-center">
                <CheckIcon className="h-4 w-4 text-green-500 mr-2" />
                7-day free trial
              </div>
              <div className="flex items-center">
                <CheckIcon className="h-4 w-4 text-green-500 mr-2" />
                Cancel anytime
              </div>
            </motion.div>
          </div>
        </div>
      </section>

      {/* Stats Section */}
      <section className="py-16 bg-white/50 backdrop-blur-sm">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-8">
            {stats.map((stat, index) => (
              <motion.div
                key={stat.label}
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.6, delay: index * 0.1 }}
                className="text-center"
              >
                <div className="text-4xl font-bold text-gradient mb-2">{stat.number}</div>
                <div className="text-gray-600 font-medium">{stat.label}</div>
              </motion.div>
            ))}
          </div>
        </div>
      </section>

      {/* Features Section */}
      <section className="py-20">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="text-center mb-20">
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.6 }}
            >
              <h2 className="text-4xl font-bold text-gray-900 mb-6">
                Everything you need for modern job hunting
              </h2>
              <p className="text-xl text-gray-600 max-w-3xl mx-auto">
                From AI-powered insights to real-time alerts, we've built the complete toolkit for your career success.
              </p>
            </motion.div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-8">
            {features.map((feature, index) => (
              <motion.div
                key={feature.title}
                initial={{ opacity: 0, y: 30 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.6, delay: index * 0.1 }}
                className="group"
              >
                <div className="card hover:shadow-2xl transition-all duration-500 group-hover:-translate-y-2">
                  <div className="flex items-center mb-6">
                    <div className={`flex-shrink-0 p-3 rounded-2xl bg-gradient-to-r ${feature.color} shadow-lg`}>
                      <feature.icon className="h-8 w-8 text-white" />
                    </div>
                    <h3 className="ml-4 text-xl font-bold text-gray-900">
                      {feature.title}
                    </h3>
                  </div>
                  <p className="text-gray-600 leading-relaxed">
                    {feature.description}
                  </p>
                </div>
              </motion.div>
            ))}
          </div>
        </div>
      </section>

      {/* Testimonials Section */}
      <section className="py-20 bg-gradient-to-r from-gray-50 to-blue-50">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="text-center mb-16">
            <h2 className="text-4xl font-bold text-gray-900 mb-6">
              Loved by thousands of job seekers
            </h2>
            <p className="text-xl text-gray-600">
              See what our users have to say about their success stories
            </p>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-8">
            {testimonials.map((testimonial, index) => (
              <motion.div
                key={testimonial.name}
                initial={{ opacity: 0, y: 30 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.6, delay: index * 0.2 }}
                className="card hover:shadow-2xl transition-all duration-300"
              >
                <div className="flex items-center mb-4">
                  {[...Array(5)].map((_, i) => (
                    <StarIcon key={i} className="h-5 w-5 text-yellow-400 fill-current" />
                  ))}
                </div>
                <p className="text-gray-700 mb-6 italic leading-relaxed">
                  "{testimonial.content}"
                </p>
                <div className="flex items-center">
                  <img
                    src={testimonial.image}
                    alt={testimonial.name}
                    className="w-12 h-12 rounded-full object-cover mr-4"
                  />
                  <div>
                    <div className="font-semibold text-gray-900">{testimonial.name}</div>
                    <div className="text-sm text-gray-600">{testimonial.role}</div>
                  </div>
                </div>
              </motion.div>
            ))}
          </div>
        </div>
      </section>

      {/* Pricing Section */}
      <section className="py-20">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="text-center mb-16">
            <h2 className="text-4xl font-bold text-gray-900 mb-6">
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
                initial={{ opacity: 0, y: 30 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.6, delay: index * 0.2 }}
                className={`card relative ${plan.popular ? 'ring-4 ring-primary-500/30 shadow-2xl scale-105' : ''}`}
              >
                {plan.popular && (
                  <div className="absolute -top-6 left-1/2 transform -translate-x-1/2">
                    <span className="bg-gradient-to-r from-primary-600 to-purple-600 text-white px-6 py-2 rounded-full text-sm font-bold shadow-lg">
                      Most Popular
                    </span>
                  </div>
                )}
                
                <div className="text-center mb-8">
                  <h3 className="text-2xl font-bold text-gray-900 mb-4">{plan.name}</h3>
                  <div className="mb-4">
                    <span className="text-5xl font-bold text-gradient">{plan.price}</span>
                    <span className="text-gray-600 ml-2">/{plan.period}</span>
                  </div>
                  <p className="text-gray-600">{plan.description}</p>
                </div>

                <ul className="space-y-4 mb-8">
                  {plan.features.map((feature) => (
                    <li key={feature} className="flex items-center">
                      <CheckIcon className="h-5 w-5 text-green-500 mr-3 flex-shrink-0" />
                      <span className="text-gray-700">{feature}</span>
                    </li>
                  ))}
                </ul>

                <Link
                  to="/auth/signup"
                  className={`w-full text-center py-4 px-6 rounded-xl font-semibold transition-all duration-300 block transform hover:-translate-y-1 ${
                    plan.popular
                      ? 'bg-gradient-to-r from-primary-600 to-purple-600 hover:from-primary-700 hover:to-purple-700 text-white shadow-lg hover:shadow-xl'
                      : 'bg-gray-100 hover:bg-gray-200 text-gray-900 shadow-sm hover:shadow-md'
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
      <section className="py-20 bg-gradient-to-br from-primary-600 via-purple-600 to-secondary-600 relative overflow-hidden">
        <div className="absolute inset-0 bg-black/10"></div>
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 text-center relative">
          <motion.div
            initial={{ opacity: 0, y: 30 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.8 }}
          >
            <h2 className="text-4xl font-bold text-white mb-6">
              Ready to transform your job search?
            </h2>
            <p className="text-xl text-blue-100 mb-10 max-w-2xl mx-auto">
              Join thousands of professionals who've found their dream jobs with JobScout Pro.
            </p>
            <div className="flex flex-col sm:flex-row gap-4 justify-center items-center">
              <Link
                to="/auth/signup"
                className="bg-white text-primary-600 hover:bg-gray-50 font-bold py-4 px-8 rounded-xl transition-all duration-300 inline-flex items-center shadow-xl hover:shadow-2xl transform hover:-translate-y-1"
              >
                Start Your Free Trial
                <ArrowRightIcon className="ml-2 h-5 w-5" />
              </Link>
              <button className="text-white hover:text-blue-100 font-semibold py-4 px-8 rounded-xl transition-colors duration-300 inline-flex items-center">
                <PlayIcon className="mr-2 h-5 w-5" />
                Watch Demo
              </button>
            </div>
          </motion.div>
        </div>
      </section>

      {/* Footer */}
      <footer className="bg-gray-900 text-white py-16">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="text-center">
            <h3 className="text-3xl font-bold mb-4 text-gradient">JobScout Pro</h3>
            <p className="text-gray-400 mb-8 text-lg">
              Your AI-powered career intelligence platform
            </p>
            <div className="flex justify-center space-x-8 text-gray-400 mb-8">
              <Link to="/privacy" className="hover:text-white transition-colors">Privacy Policy</Link>
              <Link to="/terms" className="hover:text-white transition-colors">Terms of Service</Link>
              <Link to="/contact" className="hover:text-white transition-colors">Contact</Link>
            </div>
            <div className="pt-8 border-t border-gray-800 text-center text-gray-400">
              <p>&copy; 2024 JobScout Pro. All rights reserved.</p>
            </div>
          </div>
        </div>
      </footer>
    </div>
  )
}