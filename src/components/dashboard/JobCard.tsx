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
      <div className="card p-4 hover:shadow-md transition-shadow duration-200">
        <div className="flex items-start justify-between">
          <div className="flex-1">
            <div className="flex items-center space-x-2">
              <h3 className="font-medium text-gray-900 text-sm">{job.title}</h3>
              {job.isNew && (
                <span className="px-2 py-1 text-xs bg-primary-100 text-primary-800 rounded-full">
                  New
                </span>
              )}
            </div>
            <p className="text-sm text-gray-600 mt-1">{job.company}</p>
            <div className="flex items-center text-xs text-gray-500 mt-2 space-x-3">
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
              <div className="flex items-center text-xs text-success-600 mt-1">
                <BanknotesIcon className="h-3 w-3 mr-1" />
                {job.salary}
              </div>
            )}
          </div>
          <button
            onClick={toggleSave}
            className="p-1 text-gray-400 hover:text-primary-600 transition-colors"
          >
            {isSaved ? (
              <BookmarkSolidIcon className="h-4 w-4 text-primary-600" />
            ) : (
              <BookmarkIcon className="h-4 w-4" />
            )}
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="card hover:shadow-md transition-shadow duration-200">
      <div className="flex items-start justify-between">
        <div className="flex-1">
          <div className="flex items-center space-x-2">
            <h3 className="font-semibold text-gray-900">{job.title}</h3>
            {job.isNew && (
              <span className="px-2 py-1 text-xs bg-primary-100 text-primary-800 rounded-full">
                New
              </span>
            )}
          </div>
          <p className="text-gray-600 mt-1">{job.company}</p>
          
          <div className="flex items-center text-sm text-gray-500 mt-2 space-x-4">
            <div className="flex items-center">
              <MapPinIcon className="h-4 w-4 mr-1" />
              {job.location}
            </div>
            <div className="flex items-center">
              <ClockIcon className="h-4 w-4 mr-1" />
              {job.postedDate}
            </div>
          </div>

          {job.salary && (
            <div className="flex items-center text-sm text-success-600 mt-2">
              <BanknotesIcon className="h-4 w-4 mr-1" />
              {job.salary}
            </div>
          )}

          {job.description && (
            <p className="text-gray-600 text-sm mt-3 line-clamp-2">
              {job.description}
            </p>
          )}
        </div>

        <div className="flex items-center space-x-2 ml-4">
          <button
            onClick={toggleSave}
            className="p-2 text-gray-400 hover:text-primary-600 transition-colors"
          >
            {isSaved ? (
              <BookmarkSolidIcon className="h-5 w-5 text-primary-600" />
            ) : (
              <BookmarkIcon className="h-5 w-5" />
            )}
          </button>
          {job.url && (
            <a
              href={job.url}
              target="_blank"
              rel="noopener noreferrer"
              className="p-2 text-gray-400 hover:text-primary-600 transition-colors"
            >
              <ArrowTopRightOnSquareIcon className="h-5 w-5" />
            </a>
          )}
        </div>
      </div>

      <div className="mt-4 flex items-center justify-between pt-4 border-t border-gray-200">
        <Link
          to={`/dashboard/jobs/${job.id}`}
          className="text-primary-600 hover:text-primary-700 font-medium text-sm"
        >
          View Details
        </Link>
        <div className="flex items-center space-x-2">
          <button className="btn-secondary text-sm py-1 px-3">
            Save for Later
          </button>
          <button className="btn-primary text-sm py-1 px-3">
            Apply Now
          </button>
        </div>
      </div>
    </div>
  )
}