'use client'

import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { Link, useNavigate } from 'react-router-dom'
import { EyeIcon, EyeSlashIcon } from '@heroicons/react/24/outline'
import toast from 'react-hot-toast'

const signupSchema = z.object({
  firstName: z.string().min(2, 'First name must be at least 2 characters'),
  lastName: z.string().min(2, 'Last name must be at least 2 characters'),
  email: z.string().email('Please enter a valid email address'),
  password: z.string().min(8, 'Password must be at least 8 characters'),
  confirmPassword: z.string(),
  terms: z.boolean().refine(val => val === true, 'You must accept the terms and conditions'),
}).refine((data) => data.password === data.confirmPassword, {
  message: "Passwords don't match",
  path: ["confirmPassword"],
})

type SignupFormData = z.infer<typeof signupSchema>

export default function SignupForm() {
  const [showPassword, setShowPassword] = useState(false)
  const [showConfirmPassword, setShowConfirmPassword] = useState(false)
  const [isLoading, setIsLoading] = useState(false)
  const navigate = useNavigate()

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<SignupFormData>({
    resolver: zodResolver(signupSchema),
  })

  const onSubmit = async (data: SignupFormData) => {
    setIsLoading(true)
    try {
      // For demo purposes, accept any valid form data
      console.log('Signup data:', data)
      
      // Simulate API call
      await new Promise(resolve => setTimeout(resolve, 1000))
      
      // Store user session
      localStorage.setItem('user', JSON.stringify({
        email: data.email,
        name: `${data.firstName} ${data.lastName}`,
        loggedIn: true
      }))
      
      toast.success('Account created successfully!')
      navigate('/dashboard')
    } catch (error) {
      toast.error('Failed to create account. Please try again.')
    } finally {
      setIsLoading(false)
    }
  }

  return (
    <form className="space-y-6" onSubmit={handleSubmit(onSubmit)}>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label htmlFor="firstName" className="label">
            First name
          </label>
          <input
            {...register('firstName')}
            type="text"
            className="input-field"
            placeholder="John"
          />
          {errors.firstName && (
            <p className="mt-2 text-sm text-red-600 font-medium">{errors.firstName.message}</p>
          )}
        </div>

        <div>
          <label htmlFor="lastName" className="label">
            Last name
          </label>
          <input
            {...register('lastName')}
            type="text"
            className="input-field"
            placeholder="Doe"
          />
          {errors.lastName && (
            <p className="mt-2 text-sm text-red-600 font-medium">{errors.lastName.message}</p>
          )}
        </div>
      </div>

      <div>
        <label htmlFor="email" className="label">
          Email address
        </label>
        <input
          {...register('email')}
          type="email"
          className="input-field"
          placeholder="john@example.com"
        />
        {errors.email && (
          <p className="mt-2 text-sm text-red-600 font-medium">{errors.email.message}</p>
        )}
      </div>

      <div>
        <label htmlFor="password" className="label">
          Password
        </label>
        <div className="relative">
          <input
            {...register('password')}
            type={showPassword ? 'text' : 'password'}
            className="input-field pr-12"
            placeholder="••••••••"
          />
          <button
            type="button"
            className="absolute inset-y-0 right-0 pr-4 flex items-center text-gray-400 hover:text-gray-600 transition-colors"
            onClick={() => setShowPassword(!showPassword)}
          >
            {showPassword ? (
              <EyeSlashIcon className="h-5 w-5" />
            ) : (
              <EyeIcon className="h-5 w-5" />
            )}
          </button>
        </div>
        {errors.password && (
          <p className="mt-2 text-sm text-red-600 font-medium">{errors.password.message}</p>
        )}
      </div>

      <div>
        <label htmlFor="confirmPassword" className="label">
          Confirm password
        </label>
        <div className="relative">
          <input
            {...register('confirmPassword')}
            type={showConfirmPassword ? 'text' : 'password'}
            className="input-field pr-12"
            placeholder="••••••••"
          />
          <button
            type="button"
            className="absolute inset-y-0 right-0 pr-4 flex items-center text-gray-400 hover:text-gray-600 transition-colors"
            onClick={() => setShowConfirmPassword(!showConfirmPassword)}
          >
            {showConfirmPassword ? (
              <EyeSlashIcon className="h-5 w-5" />
            ) : (
              <EyeIcon className="h-5 w-5" />
            )}
          </button>
        </div>
        {errors.confirmPassword && (
          <p className="mt-2 text-sm text-red-600 font-medium">{errors.confirmPassword.message}</p>
        )}
      </div>

      <div className="flex items-center">
        <input
          {...register('terms')}
          type="checkbox"
          className="h-4 w-4 text-primary-600 focus:ring-primary-500 border-gray-300 rounded"
        />
        <label className="ml-3 block text-sm text-gray-900 font-medium">
          I agree to the{' '}
          <Link to="/terms" className="text-primary-600 hover:text-primary-500 font-semibold transition-colors">
            Terms and Conditions
          </Link>{' '}
          and{' '}
          <Link to="/privacy" className="text-primary-600 hover:text-primary-500 font-semibold transition-colors">
            Privacy Policy
          </Link>
        </label>
      </div>
      {errors.terms && (
        <p className="text-sm text-red-600 font-medium">{errors.terms.message}</p>
      )}

      <div>
        <button
          type="submit"
          disabled={isLoading}
          className="w-full btn-primary disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {isLoading ? (
            <div className="flex items-center justify-center">
              <div className="animate-spin rounded-full h-5 w-5 border-b-2 border-white mr-2"></div>
              Creating account...
            </div>
          ) : (
            'Create account'
          )}
        </button>
      </div>

      <div className="text-center">
        <p className="text-sm text-gray-600">
          Already have an account?{' '}
          <Link to="/auth/login" className="text-primary-600 hover:text-primary-500 font-semibold transition-colors">
            Sign in
          </Link>
        </p>
      </div>

      {/* Demo info */}
      <div className="mt-6 p-4 bg-gradient-to-r from-blue-50 to-indigo-50 rounded-xl border border-blue-200/50">
        <p className="text-sm text-blue-800 font-medium">
          <strong>Demo Mode:</strong> Fill out the form to create an account and explore the job search features.
        </p>
      </div>
    </form>
  )
}