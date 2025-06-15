'use client'

import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  MapPinIcon,
  ClockIcon,
  BookmarkIcon,
  ArrowTopRightOnSquareIcon,
  BanknotesIcon
} from '@heroicons/react/24/outline'
import { BookmarkIcon as BookmarkSolidIcon } from '@heroicons/react/24/solid'

interface Job {
  id: string
  title: string
  company: string
  location: string
  salary?: string
  postedDate: string
  isNew: boolean
  isSaved: boolean
  description?: string
  url?: string
}

interface JobCardProps {
  job: Job
  compact?: boolean
}

export default function JobCard({ job, compact = false }: JobCardProps) {
  const [isSaved, setIsSaved] = useState(job.isSaved)

  const toggleSave = async () => {
    try {
      // TODO: Implement API call to save/unsave job
      setIsSaved(!isSaved)
    } catch (error) {
      console.error('Error toggling job save:', error)
    }
  }

  if (compact) {
    return (
      <div className="card p-6 hover:shadow-2xl transition-all duration-300 group hover:-translate-y-1 border border-white/20">
        <div className="flex items-start justify-between">
          <div className="flex-1">
            <div className="flex items-center space-x-3">
              <h3 className="font-semibold text-gray-900 text-sm group-hover:text-primary-600 transition-colors">{job.title}</h3>
              {job.isNew && (
                <span className="px-2 py-1 text-xs bg-gradient-to-r from-green-500 to-emerald-500 text-white rounded-full font-semibold shadow-sm">
                  New
                </span>
              )}
            </div>
            <p className="text-sm text-gray-600 mt-1 font-medium">{job.company}</p>
            <div className="flex items-center text-xs text-gray-500 mt-3 space-x-4">
              <div className="flex items-center">
                <MapPinIcon className="h-3 w-3 mr-1" />
                {job.location}
              </div>
              <div className="flex items-center">
                <ClockIcon className="h-3 w-3 mr-1" />
                {job.postedDate}
              </div>
            </div>
            {job.salary && (
              <div className="flex items-center text-xs text-green-600 mt-2 font-semibold">
                <BanknotesIcon className="h-3 w-3 mr-1" />
                {job.salary}
              </div>
            )}
          </div>
          <button
            onClick={toggleSave}
            className="p-2 text-gray-400 hover:text-primary-600 transition-colors rounded-lg hover:bg-primary-50"
          >
            {isSaved ? (
              <BookmarkSolidIcon className="h-5 w-5 text-primary-600" />
            ) : (
              <BookmarkIcon className="h-5 w-5" />
            )}
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="card hover:shadow-2xl transition-all duration-300 group hover:-translate-y-1 border border-white/20">
      <div className="flex items-start justify-between">
        <div className="flex-1">
          <div className="flex items-center space-x-3">
            <h3 className="font-bold text-gray-900 text-lg group-hover:text-primary-600 transition-colors">{job.title}</h3>
            {job.isNew && (
              <span className="px-3 py-1 text-xs bg-gradient-to-r from-green-500 to-emerald-500 text-white rounded-full font-semibold shadow-sm">
                New
              </span>
            )}
          </div>
          <p className="text-gray-600 mt-2 font-semibold">{job.company}</p>
          
          <div className="flex items-center text-sm text-gray-500 mt-3 space-x-6">
            <div className="flex items-center">
              <MapPinIcon className="h-4 w-4 mr-2" />
              {job.location}
            </div>
            <div className="flex items-center">
              <ClockIcon className="h-4 w-4 mr-2" />
              {job.postedDate}
            </div>
          </div>

          {job.salary && (
            <div className="flex items-center text-sm text-green-600 mt-3 font-semibold">
              <BanknotesIcon className="h-4 w-4 mr-2" />
              {job.salary}
            </div>
          )}

          {job.description && (
            <p className="text-gray-600 text-sm mt-4 line-clamp-2 leading-relaxed">
              {job.description}
            </p>
          )}
        </div>

        <div className="flex items-center space-x-2 ml-6">
          <button
            onClick={toggleSave}
            className="p-3 text-gray-400 hover:text-primary-600 transition-colors rounded-xl hover:bg-primary-50"
          >
            {isSaved ? (
              <BookmarkSolidIcon className="h-6 w-6 text-primary-600" />
            ) : (
              <BookmarkIcon className="h-6 w-6" />
            )}
          </button>
          {job.url && (
            <a
              href={job.url}
              target="_blank"
              rel="noopener noreferrer"
              className="p-3 text-gray-400 hover:text-primary-600 transition-colors rounded-xl hover:bg-primary-50"
            >
              <ArrowTopRightOnSquareIcon className="h-6 w-6" />
            </a>
          )}
        </div>
      </div>

      <div className="mt-6 flex items-center justify-between pt-6 border-t border-gray-200/50">
        <Link
          to={`/dashboard/jobs/${job.id}`}
          className="text-primary-600 hover:text-primary-700 font-semibold text-sm transition-colors"
        >
          View Details
        </Link>
        <div className="flex items-center space-x-3">
          <button className="btn-secondary text-sm py-2 px-4">
            Save for Later
          </button>
          <button className="btn-primary text-sm py-2 px-4">
            Apply Now
          </button>
        </div>
      </div>
    </div>
  )
}