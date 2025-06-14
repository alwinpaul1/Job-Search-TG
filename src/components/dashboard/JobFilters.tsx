'use client'

import { useState } from 'react'
import { motion } from 'framer-motion'
import { XMarkIcon } from '@heroicons/react/24/outline'
import { 
  JOB_EXPERIENCE_LEVELS, 
  JOB_TYPES, 
  WORKPLACE_TYPES,
  INDUSTRIES,
  DATE_FILTERS
} from '@/lib/constants'

interface JobFiltersProps {
  onFiltersChange: (filters: any) => void
}

export default function JobFilters({ onFiltersChange }: JobFiltersProps) {
  const [filters, setFilters] = useState({
    experience: [] as string[],
    jobType: [] as string[],
    workplace: [] as string[],
    industry: [] as string[],
    datePosted: '',
    salaryMin: '',
    salaryMax: '',
    location: ''
  })

  const updateFilter = (key: string, value: any) => {
    const newFilters = { ...filters, [key]: value }
    setFilters(newFilters)
    onFiltersChange(newFilters)
  }

  const toggleArrayFilter = (key: string, value: string) => {
    const currentArray = filters[key as keyof typeof filters] as string[]
    const newArray = currentArray.includes(value)
      ? currentArray.filter(item => item !== value)
      : [...currentArray, value]
    updateFilter(key, newArray)
  }

  const clearAllFilters = () => {
    const clearedFilters = {
      experience: [],
      jobType: [],
      workplace: [],
      industry: [],
      datePosted: '',
      salaryMin: '',
      salaryMax: '',
      location: ''
    }
    setFilters(clearedFilters)
    onFiltersChange(clearedFilters)
  }

  const hasActiveFilters = Object.values(filters).some(value => 
    Array.isArray(value) ? value.length > 0 : value !== ''
  )

  return (
    <motion.div
      initial={{ opacity: 0, y: -20 }}
      animate={{ opacity: 1, y: 0 }}
      className="card"
    >
      <div className="flex items-center justify-between mb-6">
        <h3 className="text-lg font-semibold text-gray-900">Filters</h3>
        {hasActiveFilters && (
          <button
            onClick={clearAllFilters}
            className="text-sm text-primary-600 hover:text-primary-700 font-medium"
          >
            Clear All
          </button>
        )}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        {/* Experience Level */}
        <div>
          <label className="label">Experience Level</label>
          <div className="space-y-2">
            {JOB_EXPERIENCE_LEVELS.map((level) => (
              <label key={level.value} className="flex items-center">
                <input
                  type="checkbox"
                  checked={filters.experience.includes(level.value)}
                  onChange={() => toggleArrayFilter('experience', level.value)}
                  className="h-4 w-4 text-primary-600 focus:ring-primary-500 border-gray-300 rounded"
                />
                <span className="ml-2 text-sm text-gray-700">{level.label}</span>
              </label>
            ))}
          </div>
        </div>

        {/* Job Type */}
        <div>
          <label className="label">Job Type</label>
          <div className="space-y-2">
            {JOB_TYPES.map((type) => (
              <label key={type.value} className="flex items-center">
                <input
                  type="checkbox"
                  checked={filters.jobType.includes(type.value)}
                  onChange={() => toggleArrayFilter('jobType', type.value)}
                  className="h-4 w-4 text-primary-600 focus:ring-primary-500 border-gray-300 rounded"
                />
                <span className="ml-2 text-sm text-gray-700">{type.label}</span>
              </label>
            ))}
          </div>
        </div>

        {/* Workplace Type */}
        <div>
          <label className="label">Workplace</label>
          <div className="space-y-2">
            {WORKPLACE_TYPES.map((type) => (
              <label key={type.value} className="flex items-center">
                <input
                  type="checkbox"
                  checked={filters.workplace.includes(type.value)}
                  onChange={() => toggleArrayFilter('workplace', type.value)}
                  className="h-4 w-4 text-primary-600 focus:ring-primary-500 border-gray-300 rounded"
                />
                <span className="ml-2 text-sm text-gray-700">{type.label}</span>
              </label>
            ))}
          </div>
        </div>

        {/* Date Posted */}
        <div>
          <label className="label">Date Posted</label>
          <select
            value={filters.datePosted}
            onChange={(e) => updateFilter('datePosted', e.target.value)}
            className="input-field"
          >
            <option value="">Any time</option>
            {DATE_FILTERS.map((filter) => (
              <option key={filter.value} value={filter.value}>
                {filter.label}
              </option>
            ))}
          </select>
        </div>

        {/* Salary Range */}
        <div>
          <label className="label">Salary Range (USD)</label>
          <div className="grid grid-cols-2 gap-2">
            <input
              type="number"
              placeholder="Min"
              value={filters.salaryMin}
              onChange={(e) => updateFilter('salaryMin', e.target.value)}
              className="input-field"
              min="0"
              step="1000"
            />
            <input
              type="number"
              placeholder="Max"
              value={filters.salaryMax}
              onChange={(e) => updateFilter('salaryMax', e.target.value)}
              className="input-field"
              min="0"
              step="1000"
            />
          </div>
        </div>

        {/* Location */}
        <div>
          <label className="label">Location</label>
          <input
            type="text"
            placeholder="City, State or Remote"
            value={filters.location}
            onChange={(e) => updateFilter('location', e.target.value)}
            className="input-field"
          />
        </div>
      </div>

      {/* Active Filters */}
      {hasActiveFilters && (
        <div className="mt-6 pt-6 border-t border-gray-200">
          <div className="flex flex-wrap gap-2">
            {filters.experience.map((exp) => (
              <span
                key={exp}
                className="inline-flex items-center px-3 py-1 rounded-full text-sm bg-primary-100 text-primary-800"
              >
                {JOB_EXPERIENCE_LEVELS.find(l => l.value === exp)?.label}
                <button
                  onClick={() => toggleArrayFilter('experience', exp)}
                  className="ml-2 text-primary-600 hover:text-primary-800"
                >
                  <XMarkIcon className="h-4 w-4" />
                </button>
              </span>
            ))}
            {filters.jobType.map((type) => (
              <span
                key={type}
                className="inline-flex items-center px-3 py-1 rounded-full text-sm bg-primary-100 text-primary-800"
              >
                {JOB_TYPES.find(t => t.value === type)?.label}
                <button
                  onClick={() => toggleArrayFilter('jobType', type)}
                  className="ml-2 text-primary-600 hover:text-primary-800"
                >
                  <XMarkIcon className="h-4 w-4" />
                </button>
              </span>
            ))}
            {filters.workplace.map((workplace) => (
              <span
                key={workplace}
                className="inline-flex items-center px-3 py-1 rounded-full text-sm bg-primary-100 text-primary-800"
              >
                {WORKPLACE_TYPES.find(w => w.value === workplace)?.label}
                <button
                  onClick={() => toggleArrayFilter('workplace', workplace)}
                  className="ml-2 text-primary-600 hover:text-primary-800"
                >
                  <XMarkIcon className="h-4 w-4" />
                </button>
              </span>
            ))}
          </div>
        </div>
      )}
    </motion.div>
  )
}