import { NextRequest, NextResponse } from 'next/server'
import JobScraper from '@/lib/jobScraper'

export async function POST(request: NextRequest) {
  try {
    const body = await request.json()
    const { keywords, location } = body

    // Validate required fields
    if (!keywords || !Array.isArray(keywords) || keywords.length === 0) {
      return NextResponse.json(
        { error: 'Keywords are required and must be an array' },
        { status: 400 }
      )
    }

    if (!location) {
      return NextResponse.json(
        { error: 'Location is required' },
        { status: 400 }
      )
    }

    // Initialize the job scraper
    const scraper = new JobScraper()

    console.log('Analyzing job market for:', keywords, location)

    // Analyze job market
    const marketAnalysis = await scraper.analyzeJobMarket(keywords, location)

    console.log('Market analysis completed:', marketAnalysis)

    return NextResponse.json({
      success: true,
      data: marketAnalysis
    })

  } catch (error) {
    console.error('Error in market analysis API:', error)
    
    return NextResponse.json(
      { 
        error: 'Failed to analyze job market',
        message: error instanceof Error ? error.message : 'Unknown error'
      },
      { status: 500 }
    )
  }
}