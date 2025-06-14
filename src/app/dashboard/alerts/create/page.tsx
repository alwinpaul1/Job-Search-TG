'use client'

import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { useRouter } from 'next/navigation'
import { motion } from 'framer-motion'
import { 
  XMarkIcon,
  PlusIcon,
  MagnifyingGlassIcon
} from '@heroicons/react/24/outline'
import DashboardLayout from '@/components/dashboard/DashboardLayout'
import toast from 'react-hot-toast'
import { 
  JOB_EXPERIENCE_LEVELS, 
  JOB_TYPES, 
  WORKPLACE_TYPES, 
  ALERT_FREQUENCIES,
  POPULAR_SKILLS,
  POPULAR_LOCATIONS
} from '@/lib/constants'

const createAlertSchema = z.object({
  name: z.string().min(1, 'Alert name is required'),
  keywords: z.array(z.string()).min(1, 'At least one keyword is required'),
  location: z.string().min(1, 'Location is required'),
  experience: z.string().optional(),
  jobType: z.string().optional(),
  workplace: z.string().optional(),
  salaryMin: z.number().optional(),
  salaryMax: z.number().optional(),
  frequency: z.enum(['realtime', 'daily', 'weekly']),
})

type CreateAlertFormData = z.infer<typeof createAlertSchema>

export default function CreateAlertPage() {
  const router = useRouter()
  const [keywords, setKeywords] = useState<string[]>([])
  const [keywordInput, setKeywordInput] = useState('')
  const [isLoading, setIsLoading] = useState(false)

  const {
    register,
    handleSubmit,
    formState: { errors },
    setValue,
    watch
  } = useForm<CreateAlertFormData>({
    resolver: zodResolver(createAlertSchema),
    defaultValues: {
      frequency: 'daily'
    }
  })

  const addKeyword = (keyword: string) => {
    if (keyword && !keywords.includes(keyword)) {
      const newKeywords = [...keywords, keyword]
      setKeywords(newKeywords)
      setValue('keywords', newKeywords)
      setKeywordInput('')
    }
  }

  const removeKeyword = (keyword: string) => {
    const newKeywords = keywords.filter(k => k !== keyword)
    setKeywords(newKeywords)
    setValue('keywords', newKeywords)
  }

  const handleKeywordInputKeyPress = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      addKeyword(keywordInput.trim())
    }
  }

  const onSubmit = async (data: CreateAlertFormData) => {
    setIsLoading(true)
    try {
      // TODO: Implement actual API call
      console.log('Creating alert:', data)
      
      // Simulate API call
      await new Promise(resolve => setTimeout(resolve, 1000))
      
      toast.success('Job alert created successfully!')
      router.push('/dashboard/alerts')
    } catch (error) {
      toast.error('Failed to create job alert. Please try again.')
    } finally {
      setIsLoading(false)
    }
  }

  return (
    <DashboardLayout>
      <div className="max-w-2xl mx-auto">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          className="space-y-8"
        >
          {/* Header */}
          <div>
            <h1 className="text-3xl font-bold text-gray-900">Create Job Alert</h1>
            <p className="text-gray-600 mt-1">Set up a new job alert to find opportunities that match your criteria.</p>
          </div>

          {/* Form */}
          <form onSubmit={handleSubmit(onSubmit)} className="space-y-6">
            <div className="card">
              <h2 className="text-lg font-semibold text-gray-900 mb-6">Basic Information</h2>
              
              {/* Alert Name */}
              <div>
                <label className="label">Alert Name</label>
                <input
                  {...register('name')}
                  type="text"
                  className="input-field"
                  placeholder="e.g., Senior React Developer Jobs"
                />
                {errors.name && (
                  <p className="mt-1 text-sm text-error-600">{errors.name.message}</p>
                )}
              </div>

              {/* Keywords */}
              <div>
                <label className="label">Keywords</label>
                <div className="space-y-3">
                  <div className="flex">
                    <input
                      type="text"
                      value={keywordInput}
                      onChange={(e) => setKeywordInput(e.target.value)}
                      onKeyPress={handleKeywordInputKeyPress}
                      className="input-field rounded-r-none"
                      placeholder="Add skills, job titles, or technologies"
                    />
                    <button
                      type="button"
                      onClick={() => addKeyword(keywordInput.trim())}
                      className="px-4 py-2 bg-primary-600 text-white rounded-r-lg hover:bg-primary-700 transition-colors"
                    >
                      <PlusIcon className="h-5 w-5" />
                    </button>
                  </div>
                  
                  {/* Popular Skills */}
                  <div>
                    <p className="text-sm text-gray-600 mb-2">Popular skills:</p>
                    <div className="flex flex-wrap gap-2">
                      {POPULAR_SKILLS.slice(0, 10).map((skill) => (
                        <button
                          key={skill}
                          type="button"
                          onClick={() => addKeyword(skill)}
                          className="px-3 py-1 text-sm bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 transition-colors"
                        >
                          {skill}
                        </button>
                      ))}
                    </div>
                  </div>

                  {/* Selected Keywords */}
                  {keywords.length > 0 && (
                    <div>
                      <p className="text-sm text-gray-600 mb-2">Selected keywords:</p>
                      <div className="flex flex-wrap gap-2">
                        {keywords.map((keyword) => (
                          <span
                            key={keyword}
                            className="flex items-center px-3 py-1 bg-primary-100 text-primary-800 rounded-lg"
                          >
                            {keyword}
                            <button
                              type="button"
                              onClick={() => removeKeyword(keyword)}
                              className="ml-2 text-primary-600 hover:text-primary-800"
                            >
                              <XMarkIcon className="h-4 w-4" />
                            </button>
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
                {errors.keywords && (
                  <p className="mt-1 text-sm text-error-600">{errors.keywords.message}</p>
                )}
              </div>

              {/* Location */}
              <div>
                <label className="label">Location</label>
                <input
                  {...register('location')}
                  type="text"
                  className="input-field"
                  placeholder="e.g., San Francisco, CA or Remote"
                  list="popular-locations"
                />
                <datalist id="popular-locations">
                  {POPULAR_LOCATIONS.map((location) => (
                    <option key={location} value={location} />
                  ))}
                </datalist>
                {errors.location && (
                  <p className="mt-1 text-sm text-error-600">{errors.location.message}</p>
                )}
              </div>
            </div>

            {/* Filters */}
            <div className="card">
              <h2 className="text-lg font-semibold text-gray-900 mb-6">Filters (Optional)</h2>
              
              <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                {/* Experience Level */}
                <div>
                  <label className="label">Experience Level</label>
                  <select {...register('experience')} className="input-field">
                    <option value="">Any experience level</option>
                    {JOB_EXPERIENCE_LEVELS.map((level) => (
                      <option key={level.value} value={level.value}>
                        {level.label}
                      </option>
                    ))}
                  </select>
                </div>

                {/* Job Type */}
                <div>
                  <label className="label">Job Type</label>
                  <select {...register('jobType')} className="input-field">
                    <option value="">Any job type</option>
                    {JOB_TYPES.map((type) => (
                      <option key={type.value} value={type.value}>
                        {type.label}
                      </option>
                    ))}
                  </select>
                </div>

                {/* Workplace Type */}
                <div>
                  <label className="label">Workplace</label>
                  <select {...register('workplace')} className="input-field">
                    <option value="">Any workplace type</option>
                    {WORKPLACE_TYPES.map((type) => (
                      <option key={type.value} value={type.value}>
                        {type.label}
                      </option>
                    ))}
                  </select>
                </div>

                {/* Alert Frequency */}
                <div>
                  <label className="label">Check Frequency</label>
                  <select {...register('frequency')} className="input-field">
                    {ALERT_FREQUENCIES.map((freq) => (
                      <option key={freq.value} value={freq.value}>
                        {freq.label}
                      </option>
                    ))}
                  </select>
                </div>
              </div>

              {/* Salary Range */}
              <div>
                <label className="label">Salary Range (USD)</label>
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <input
                      {...register('salaryMin', { valueAsNumber: true })}
                      type="number"
                      className="input-field"
                      placeholder="Min salary"
                      min="0"
                      step="1000"
                    />
                  </div>
                  <div>
                    <input
                      {...register('salaryMax', { valueAsNumber: true })}
                      type="number"
                      className="input-field"
                      placeholder="Max salary"
                      min="0"
                      step="1000"
                    />
                  </div>
                </div>
              </div>
            </div>

            {/* Actions */}
            <div className="flex justify-end space-x-4">
              <button
                type="button"
                onClick={() => router.back()}
                className="btn-secondary"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={isLoading}
                className="btn-primary disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {isLoading ? 'Creating...' : 'Create Alert'}
              </button>
            </div>
          </form>
        </motion.div>
      </div>
    </DashboardLayout>
  )
}