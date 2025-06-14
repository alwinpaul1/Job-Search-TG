'use client'

import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { motion } from 'framer-motion'
import { 
  MagnifyingGlassIcon,
  PlusIcon,
  XMarkIcon
} from '@heroicons/react/24/outline'
import DashboardLayout from '@/components/dashboard/DashboardLayout'
import JobCard from '@/components/dashboard/JobCard'
import toast from 'react-hot-toast'
import { 
  JOB_EXPERIENCE_LEVELS, 
  JOB_TYPES, 
  WORKPLACE_TYPES,
  POPULAR_SKILLS,
  POPULAR_LOCATIONS
} from '@/lib/constants'

const searchSchema = z.object({
  keywords: z.array(z.string()).min(1, 'At least one keyword is required'),
  location: z.string().min(1, 'Location is required'),
  experience: z.string().optional(),
  jobType: z.string().optional(),
  workplace: z.string().optional(),
})

type SearchFormData = z.infer<typeof searchSchema>

interface Job {
  id: string
  title: string
  company: string
  location: string
  salary?: string
  postedDate: string
  isNew: boolean
  isSaved: boolean
  description: string
  url: string
  skills: string[]
}

export default function JobSearchPage() {
  const [keywords, setKeywords] = useState<string[]>([])
  const [keywordInput, setKeywordInput] = useState('')
  const [jobs, setJobs] = useState<Job[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [hasSearched, setHasSearched] = useState(false)

  const {
    register,
    handleSubmit,
    formState: { errors },
    setValue,
    watch
  } = useForm<SearchFormData>({
    resolver: zodResolver(searchSchema),
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

  const onSubmit = async (data: SearchFormData) => {
    setIsLoading(true)
    setHasSearched(true)
    
    try {
      console.log('Searching for jobs with:', data)
      
      const response = await fetch('/api/jobs/search', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(data),
      })

      if (!response.ok) {
        throw new Error('Failed to search jobs')
      }

      const result = await response.json()
      
      if (result.success) {
        setJobs(result.data)
        toast.success(`Found ${result.count} jobs!`)
      } else {
        throw new Error(result.error || 'Failed to search jobs')
      }
    } catch (error) {
      console.error('Error searching jobs:', error)
      toast.error('Failed to search jobs. Please try again.')
    } finally {
      setIsLoading(false)
    }
  }

  return (
    <DashboardLayout>
      <div className="max-w-6xl mx-auto space-y-8">
        {/* Header */}
        <div>
          <h1 className="text-3xl font-bold text-gray-900">Job Search</h1>
          <p className="text-gray-600 mt-1">Find your next opportunity with AI-powered job matching.</p>
        </div>

        {/* Search Form */}
        <div className="card">
          <form onSubmit={handleSubmit(onSubmit)} className="space-y-6">
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
              {/* Keywords */}
              <div className="lg:col-span-2">
                <label className="label">Keywords *</label>
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
                      {POPULAR_SKILLS.slice(0, 8).map((skill) => (
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
                <label className="label">Location *</label>
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
            </div>

            {/* Search Button */}
            <div className="flex justify-center">
              <button
                type="submit"
                disabled={isLoading}
                className="btn-primary flex items-center px-8 py-3 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {isLoading ? (
                  <>
                    <div className="animate-spin rounded-full h-5 w-5 border-b-2 border-white mr-2"></div>
                    Searching...
                  </>
                ) : (
                  <>
                    <MagnifyingGlassIcon className="h-5 w-5 mr-2" />
                    Search Jobs
                  </>
                )}
              </button>
            </div>
          </form>
        </div>

        {/* Results */}
        {hasSearched && (
          <div className="space-y-6">
            <div className="flex items-center justify-between">
              <h2 className="text-xl font-semibold text-gray-900">
                Search Results
                {jobs.length > 0 && (
                  <span className="text-gray-600 font-normal ml-2">
                    ({jobs.length} job{jobs.length !== 1 ? 's' : ''} found)
                  </span>
                )}
              </h2>
            </div>

            {jobs.length > 0 ? (
              <div className="space-y-6">
                {jobs.map((job, index) => (
                  <motion.div
                    key={job.id}
                    initial={{ opacity: 0, y: 20 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ duration: 0.3, delay: index * 0.1 }}
                  >
                    <JobCard job={job} />
                  </motion.div>
                ))}
              </div>
            ) : !isLoading ? (
              <div className="text-center py-12">
                <MagnifyingGlassIcon className="h-12 w-12 text-gray-400 mx-auto mb-4" />
                <h3 className="text-lg font-medium text-gray-900 mb-2">No jobs found</h3>
                <p className="text-gray-600">
                  Try adjusting your search criteria or keywords.
                </p>
              </div>
            ) : null}
          </div>
        )}
      </div>
    </DashboardLayout>
  )
}