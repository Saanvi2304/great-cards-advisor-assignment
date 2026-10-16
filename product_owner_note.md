# Note to Product Owner: Great.cards Advisor

## 1. Summary

I tested the card recommendation system using 20 different scenarios. The main issues I found were:

- **Wrong card recommendations:** Cards were recommended even when the user's income might not meet their eligibility requirements.
- **Incorrect savings calculations:** Joining fees were not always included, making the estimated savings look higher than they actually were.
- **Assumptions about spending:** The system sometimes assumed spending amounts or categories that the user had not provided.
- **Unverified information:** Some forex charges and card benefits were estimated without enough supporting information.
- **Reward limits:** Reward caps were not always considered while calculating savings.

## 2. Problems Users Could Face Without These Fixes

### A. Cards shown despite income requirements

A user with an annual income of ₹1.2 lakh was shown 201 cards, including cards with much higher income requirements.

For example:
- HDFC Millennia: ₹4 lakh annual income requirement.
- Tata Neu Infinity: ₹9.6 lakh.
- Swiggy BLCK: ₹12 lakh.
- IDFC FIRST Wealth: ₹36 lakh.

Even a user earning ₹8 lakh annually might be shown cards requiring ₹9.6 lakh or ₹12 lakh.

**Why this matters:** Users may waste time applying for cards they are unlikely to qualify for.

### B. Joining fees missing from savings calculations

In one example, the system showed ₹9,000 in savings for HDFC Millennia but did not include the ₹1,180 joining fee.

After subtracting the joining fee, the estimated savings would be ₹7,820 before considering welcome benefits.

**Why this matters:** Users may believe a card saves them more money than it actually does.

### C. Incorrect assumptions about spending

When a user mentioned spending ₹10,000 on Myntra, the system made assumptions about monthly spending without clearly confirming them.

**Why this matters:** Recommendations may be based on spending patterns that do not match the user's actual habits.

### D. Unsupported forex estimates

Some international transaction fees and forex-related savings were estimated without enough verified information.

**Why this matters:** Users may make decisions based on inaccurate cost estimates.

### E. Reward caps not considered

Some recommendations did not properly account for limits on reward points or cashback.

**Why this matters:** The actual rewards users receive may be lower than the system estimates.

## 3. What Was Fixed

- Improved how card eligibility issues are identified.
- Included joining fees when calculating net savings.
- Avoided making assumptions about spending without sufficient information.
- Highlighted cases where forex charges or benefits need verification.
- Considered reward limits when estimating benefits.
- Improved handling of missing or uncertain information so the system is less likely to present guesses as facts.

## 4. Response Time and Quality

The system generally took around **9–16 seconds** to respond.

- The AI model accounted for most of the response time.
- Server-side processing took around 4–5 seconds.
- Some individual checks took less than 5 milliseconds.
- Running tool calls in parallel saved around 0.7 seconds, but also introduced race-condition risks.

**Key takeaway:** Response time can be improved, but accuracy and reliable execution should not be sacrificed just to make responses faster.

## 5. Recommended Backend Improvements

### A. Improve income-based filtering

Filter cards using their verified eligibility requirements before recommending them. If eligibility information is missing, clearly tell the user instead of making assumptions.

### B. Calculate realistic savings

Include joining fees and annual fees where applicable. Show estimated rewards, fees, and final net savings separately.

### C. Ask for missing spending details

If spending information is incomplete, ask a follow-up question rather than assuming amounts or categories.

### D. Verify card information

Use reliable, up-to-date information for forex charges, reward rates, joining fees, annual fees, and reward caps.

### E. Improve data quality

Regularly check card information for missing fields, outdated terms, inconsistent values, and duplicate records.

### F. Improve error messages

When information is unavailable, explain what is missing instead of returning a confusing or misleading recommendation.

### G. Make comparisons more useful

Compare cards using the user's actual spending patterns, estimated rewards, fees, and applicable limits.

## 6. Open Product Questions

- Should cards that the user may not qualify for be hidden completely or shown with an eligibility warning?
- Should savings always be calculated after joining and annual fees?
- What should happen when a card's eligibility requirements or reward terms cannot be verified?
- Should the system ask follow-up questions when the user's spending details are incomplete?
- How frequently should card information be checked for updates?

## 7. Future Improvements for Scaling

- **Caching:** Store frequently used card information to reduce repeated processing.
- **Session management:** Use a system such as Redis if needed to manage sessions efficiently as usage grows.
- **Monitoring:** Track response times, failures, incorrect recommendations, and missing information.
- **Testing:** Add more scenarios covering low income, incomplete spending details, joining fees, reward caps, and international transactions.

These improvements can make the system more accurate, reliable, and useful as more people start using it.