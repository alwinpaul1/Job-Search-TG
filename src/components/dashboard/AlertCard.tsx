'use client'

import { useState } from 'react'
import Link from 'next/link'
import {
  BellIcon,
  MapPinIcon,
  ClockIcon,
  EyeIcon,
  PencilIcon,
  TrashIcon,
  PlayIcon,
  PauseIcon
} from '@heroicons/react/24/outline'
import { BellIcon as BellSolidIcon } from '@heroicons/react/24/solid'

interface JobAlert {
  id: string
  name: string
  keywords: string[]
  location: string
  isActive: boolean
  lastChecked: string
  matchCount: number
}

interface AlertCardProps {
  alert: JobAlert
}

export default function AlertCard({ alert }: AlertCardProps) {
  const [isActive, setIsActive] = useState(alert.isActive)

  const toggleAlert = async () => {
    try {
      // TODO: Implement API call to toggle alert
      setIsActive(!isActive)
    } catch (error) {
      console.error('Error toggling alert:', error)
    }
  }

  const deleteAlert = async () => {
    if (confirm('Are you sure you want to delete this alert?')) {
      try {
        // TODO: Implement API call to delete alert
        console.log('Delete alert:', alert.id)
      } catch (error) {
        console.error('Error deleting alert:', error)
      }
    }
  }

  return (
    <div className="card hover:shadow-md transition-shadow duration-200">
      <div className="flex items-start justify-between">
        <div className="flex items-start space-x-3">
          <div className={`p-2 rounded-lg ${isActive ? 'bg-primary-100' : 'bg-gray-100'}`}>
            {isActive ? (
              <BellSolidIcon className="h-5 w-5 text-primary-600" />
            ) : (
              <BellIcon className="h-5 w-5 text-gray-400" />
            )}
          </div>
          
          <div className="flex-1">
            <div className="flex items-center space-x-2">
              <h3 className="font-semibold text-gray-900">{alert.name}</h3>
              <span className={`px-2 py-1 text-xs rounded-full ${
                isActive 
                  ? 'bg-success-100 text-success-800' 
                  : 'bg-gray-100 text-gray-600'
              }`}>
                {isActive ? 'Active' : 'Paused'}
              </span>
            </div>
            
            <div className="mt-1 space-y-1">
              <div className="flex items-center text-sm text-gray-600">
                <MapPinIcon className="h-4 w-4 mr-1" />
                {alert.location}
              </div>
              <div className="flex items-center text-sm text-gray-600">
                <ClockIcon className="h-4 w-4 mr-1" />
                Last checked {alert.lastChecked}
              </div>
            </div>
            
            <div className="mt-2 flex flex-wrap gap-1">
              {alert.keywords.slice(0, 3).map((keyword) => (
                <span
                  key={keyword}
                  className="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded"
                >
                  {keyword}
                </span>
              ))}
              {alert.keywords.length > 3 && (
                <span className="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded">
                  +{alert.keywords.length - 3} more
                </span>
              )}
            </div>
          </div>
        </div>

        <div className="flex items-center space-x-2">
          <Link
            href={`/dashboard/jobs?alert=${alert.id}`}
            className="flex items-center px-3 py-1 text-sm bg-primary-50 text-primary-700 rounded-lg hover:bg-primary-100 transition-colors"
          >
            <EyeIcon className="h-4 w-4 mr-1" />
            {alert.matchCount} jobs
          </Link>
        </div>
      </div>

      <div className="mt-4 flex items-center justify-between pt-4 border-t border-gray-200">
        <div className="flex items-center space-x-2">
          <button
            onClick={toggleAlert}
            className={`flex items-center px-3 py-1 text-sm rounded-lg transition-colors ${
              isActive
                ? 'bg-warning-50 text-warning-700 hover:bg-warning-100'
                : 'bg-success-50 text-success-700 hover:bg-success-100'
            }`}
          >
            {isActive ? (
              <>
                <PauseIcon className="h-4 w-4 mr-1" />
                Pause
              </>
            ) : (
              <>
                <PlayIcon className="h-4 w-4 mr-1" />
                Resume
              </>
            )}
          </button>
        </div>

        <div className="flex items-center space-x-2">
          <Link
            href={`/dashboard/alerts/${alert.id}/edit`}
            className="p-2 text-gray-400 hover:text-gray-600 transition-colors"
          >
            <PencilIcon className="h-4 w-4" />
          </Link>
          <button
            onClick={deleteAlert}
            className="p-2 text-gray-400 hover:text-error-600 transition-colors"
          >
            <TrashIcon className="h-4 w-4" />
          </button>
        </div>
      </div>
    </div>
  )
}